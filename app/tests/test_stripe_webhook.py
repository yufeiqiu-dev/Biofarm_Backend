import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.checkout_session import CheckoutSession
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant


def make_product_and_variant(db_session) -> ProductVariant:
    product = Product(cat_id=f"WH-{uuid.uuid4().hex[:4]}", name="Test", description="Test")
    variant = ProductVariant(
        catalog_id=f"WH-V-{uuid.uuid4().hex[:4]}",
        size_value=Decimal("100"),
        size_unit="g",
        price=Decimal("10.00"),
        stock=5,
    )
    product.variants.append(variant)
    db_session.add(product)
    db_session.commit()
    db_session.refresh(variant)
    return variant


def make_checkout_session(db_session, pi_id: str, variant_id: uuid.UUID) -> CheckoutSession:
    session = CheckoutSession(
        stripe_pi_id=pi_id,
        user_id="wh-user",
        cart_json=json.dumps([{"variant_id": str(variant_id), "quantity": 1}]),
        shipping_json=json.dumps({
            "name": "Test", "phone": "5550000000",
            "address1": "1 Test St", "address2": None,
            "city": "TestCity", "state": "CA", "zip": "90210", "notes": None,
        }),
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def make_stripe_event(event_type: str, pi_id: str) -> MagicMock:
    event = MagicMock()
    event.type = event_type
    event.data.object.id = pi_id
    return event


def post_webhook(client, pi_id: str, event_type: str):
    with patch("app.api.v1.endpoints.stripe_webhook.verify_webhook_signature",
               return_value=make_stripe_event(event_type, pi_id)):
        return client.post(
            "/api/v1/stripe/webhook",
            content=b"fake-payload",
            headers={"stripe-signature": "t=1,v1=fake"},
        )


def test_webhook_amount_capturable_updated_creates_order(client, db_session):
    """payment_intent.amount_capturable_updated (customer authorized) creates Order from CheckoutSession."""
    from sqlalchemy import select
    pi_id = f"pi_{uuid.uuid4().hex}"
    variant = make_product_and_variant(db_session)
    make_checkout_session(db_session, pi_id, variant.id)

    response = post_webhook(client, pi_id, "payment_intent.amount_capturable_updated")

    assert response.status_code == 200
    order = db_session.scalar(select(Order).where(Order.stripe_payment_intent_id == pi_id))
    assert order is not None
    assert order.status == OrderStatus.awaiting_fulfillment
    session = db_session.scalar(select(CheckoutSession).where(CheckoutSession.stripe_pi_id == pi_id))
    assert session is None


def test_webhook_payment_succeeded_creates_order_fallback(client, db_session):
    """payment_intent.succeeded is a fallback — creates order if not already created."""
    from sqlalchemy import select
    pi_id = f"pi_{uuid.uuid4().hex}"
    variant = make_product_and_variant(db_session)
    make_checkout_session(db_session, pi_id, variant.id)

    response = post_webhook(client, pi_id, "payment_intent.succeeded")

    assert response.status_code == 200
    order = db_session.scalar(select(Order).where(Order.stripe_payment_intent_id == pi_id))
    assert order is not None
    assert order.status == OrderStatus.awaiting_fulfillment


def test_webhook_pi_cancelled_deletes_session(client, db_session):
    """payment_intent.canceled removes the CheckoutSession without creating an Order."""
    pi_id = f"pi_{uuid.uuid4().hex}"
    variant = make_product_and_variant(db_session)
    make_checkout_session(db_session, pi_id, variant.id)

    response = post_webhook(client, pi_id, "payment_intent.canceled")

    assert response.status_code == 200
    from sqlalchemy import select
    session = db_session.scalar(select(CheckoutSession).where(CheckoutSession.stripe_pi_id == pi_id))
    assert session is None
    order = db_session.scalar(select(Order).where(Order.stripe_payment_intent_id == pi_id))
    assert order is None


def test_webhook_payment_succeeded_after_order_exists_is_noop(client, db_session):
    """A duplicate or late event whose order already exists is a safe no-op.

    Both amount_capturable_updated and succeeded reach the same handler, and the
    first consumes the checkout session, so the second legitimately finds none.
    That is the normal path, not a failure.
    """
    variant = make_product_and_variant(db_session)
    pi_id = f"pi_{uuid.uuid4().hex}"
    make_checkout_session(db_session, pi_id, variant.id)

    first = post_webhook(client, pi_id, "payment_intent.amount_capturable_updated")
    assert first.status_code == 200

    # Session is gone now; the order it became stands in for it.
    second = post_webhook(client, pi_id, "payment_intent.succeeded")

    assert second.status_code == 200
    assert db_session.query(Order).filter_by(stripe_payment_intent_id=pi_id).count() == 1


def test_webhook_payment_succeeded_with_no_session_and_no_order_fails(client, db_session):
    """A paid intent with nothing behind it must not be reported as handled.

    No checkout session and no order means the customer was charged and there is
    nothing to fulfil. Answering 200 would tell Stripe the event was processed
    and burn the retry that is the only free recovery mechanism available, so
    this has to fail loudly instead.
    """
    pi_id = f"pi_{uuid.uuid4().hex}"

    response = post_webhook(client, pi_id, "payment_intent.succeeded")

    assert response.status_code == 500
    assert db_session.query(Order).filter_by(stripe_payment_intent_id=pi_id).count() == 0


def test_webhook_payment_failed_is_noop(client, db_session):
    """payment_intent.payment_failed (single decline) must not affect anything — retry is still possible."""
    pi_id = f"pi_{uuid.uuid4().hex}"
    variant = make_product_and_variant(db_session)
    make_checkout_session(db_session, pi_id, variant.id)

    response = post_webhook(client, pi_id, "payment_intent.payment_failed")

    assert response.status_code == 200
    from sqlalchemy import select
    # Session must still exist — PI is still open for retry
    session = db_session.scalar(select(CheckoutSession).where(CheckoutSession.stripe_pi_id == pi_id))
    assert session is not None


def test_webhook_retry_after_decline_succeeds(client, db_session):
    """Declined card followed by a good card: order must be created at awaiting_fulfillment."""
    from sqlalchemy import select
    pi_id = f"pi_{uuid.uuid4().hex}"
    variant = make_product_and_variant(db_session)
    make_checkout_session(db_session, pi_id, variant.id)

    # First attempt: card declined — session still intact
    post_webhook(client, pi_id, "payment_intent.payment_failed")

    from sqlalchemy import select
    session = db_session.scalar(select(CheckoutSession).where(CheckoutSession.stripe_pi_id == pi_id))
    assert session is not None  # still retryable

    # Second attempt: good card authorized
    response = post_webhook(client, pi_id, "payment_intent.amount_capturable_updated")

    assert response.status_code == 200
    order = db_session.scalar(select(Order).where(Order.stripe_payment_intent_id == pi_id))
    assert order is not None
    assert order.status == OrderStatus.awaiting_fulfillment


def test_webhook_invalid_signature_returns_400(client):
    import stripe
    with patch("app.api.v1.endpoints.stripe_webhook.verify_webhook_signature",
               side_effect=stripe.error.SignatureVerificationError("bad sig", "sig_header")):
        response = client.post(
            "/api/v1/stripe/webhook",
            content=b"fake-payload",
            headers={"stripe-signature": "bad"},
        )
    assert response.status_code == 400


# --- malformed payloads must not enter the retry schedule ---

def test_webhook_unparseable_payload_returns_400_not_500(client: TestClient):
    """construct_event raises ValueError for a body that is not valid JSON.

    Letting it escape produced a 500 - and a 500 is exactly what tells Stripe to
    retry, so a payload that can never parse was replayed for the full retry
    schedule, filling the logs and the Stripe dashboard with a failure no retry
    could fix. 400 tells Stripe not to bother.
    """
    with patch(
        "app.api.v1.endpoints.stripe_webhook.verify_webhook_signature",
        side_effect=ValueError("Invalid payload"),
    ):
        response = client.post(
            "/api/v1/stripe/webhook",
            content=b"{not json",
            headers={"stripe-signature": "t=1,v1=fake"},
        )

    assert response.status_code == 400
    assert "Malformed" in response.json()["detail"]


def test_webhook_signature_failure_is_still_distinguished_from_a_bad_body(client: TestClient):
    """Both answer 400, but for different reasons, and the detail says which."""
    import stripe

    with patch(
        "app.api.v1.endpoints.stripe_webhook.verify_webhook_signature",
        side_effect=stripe.error.SignatureVerificationError("bad sig", "sig-header"),
    ):
        response = client.post(
            "/api/v1/stripe/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=wrong"},
        )

    assert response.status_code == 400
    assert "signature" in response.json()["detail"].lower()


# --- card details go through stripe_service, not the SDK ---

def test_card_details_come_from_the_service_layer(client: TestClient, db_session):
    """The endpoint used to call stripe.PaymentMethod.retrieve directly, which
    sidesteps stripe_bypass and removes the seam every other Stripe call is
    tested through. Patching the service must be enough to control it."""
    variant = make_product_and_variant(db_session)
    pi_id = f"pi_{uuid.uuid4().hex}"
    make_checkout_session(db_session, pi_id, variant.id)

    event = make_stripe_event("payment_intent.succeeded", pi_id)
    event.data.object.payment_method = "pm_test_123"

    with patch(
        "app.api.v1.endpoints.stripe_webhook.get_card_details",
        return_value=("amex", "0005"),
    ):
        with patch(
            "app.api.v1.endpoints.stripe_webhook.verify_webhook_signature",
            return_value=event,
        ):
            response = client.post(
                "/api/v1/stripe/webhook",
                content=b"fake-payload",
                headers={"stripe-signature": "t=1,v1=fake"},
            )

    assert response.status_code == 200
    order = db_session.query(Order).filter_by(stripe_payment_intent_id=pi_id).one()
    assert order.card_brand == "amex"
    assert order.card_last4 == "0005"


def test_a_card_lookup_failure_does_not_block_the_order(client: TestClient, db_session):
    """Card brand and last4 are cosmetic. Losing them must not cost the customer
    an order they have already paid for."""
    variant = make_product_and_variant(db_session)
    pi_id = f"pi_{uuid.uuid4().hex}"
    make_checkout_session(db_session, pi_id, variant.id)

    event = make_stripe_event("payment_intent.succeeded", pi_id)
    event.data.object.payment_method = "pm_test_456"

    with patch(
        "app.services.stripe_service._get_stripe",
        side_effect=RuntimeError("Stripe is down"),
    ):
        with patch(
            "app.api.v1.endpoints.stripe_webhook.verify_webhook_signature",
            return_value=event,
        ):
            response = client.post(
                "/api/v1/stripe/webhook",
                content=b"fake-payload",
                headers={"stripe-signature": "t=1,v1=fake"},
            )

    assert response.status_code == 200
    order = db_session.query(Order).filter_by(stripe_payment_intent_id=pi_id).one()
    assert order.status == OrderStatus.awaiting_fulfillment
    assert order.card_brand == ""


def test_webhook_sold_out_voids_the_auth_and_acknowledges(client, db_session):
    """Stock ran out between the customer paying and this webhook arriving.

    There is no order to create and the card is authorised. Returning 500 is how
    Stripe is told to retry, and it would retry for days on something that can
    never succeed, leaving the authorisation live until it expired. So the
    authorisation is voided and the event acknowledged.
    """
    variant = make_product_and_variant(db_session)
    variant.stock = 0
    db_session.commit()

    pi_id = f"pi_{uuid.uuid4().hex}"
    make_checkout_session(db_session, pi_id, variant.id)

    with patch("app.api.v1.endpoints.stripe_webhook.cancel_payment_intent") as void:
        response = post_webhook(client, pi_id, "payment_intent.amount_capturable_updated")

    assert response.status_code == 200, "a 500 here buys days of pointless Stripe retries"
    void.assert_called_once_with(pi_id)

    # No order, and the session is gone so a retry does not re-attempt it.
    assert db_session.scalar(
        select(Order).where(Order.stripe_payment_intent_id == pi_id)
    ) is None
    assert db_session.scalar(
        select(CheckoutSession).where(CheckoutSession.stripe_pi_id == pi_id)
    ) is None


def test_webhook_keeps_the_session_when_the_void_fails(client, db_session):
    """If the authorisation cannot be voided, the session must survive.

    Deleting it first would send Stripe's retry down the "no session and no
    order" path, which 500s forever and never retries the void.
    """
    variant = make_product_and_variant(db_session)
    variant.stock = 0
    db_session.commit()

    pi_id = f"pi_{uuid.uuid4().hex}"
    make_checkout_session(db_session, pi_id, variant.id)

    with patch("app.api.v1.endpoints.stripe_webhook.cancel_payment_intent",
               side_effect=RuntimeError("stripe is down")):
        try:
            post_webhook(client, pi_id, "payment_intent.amount_capturable_updated")
        except RuntimeError:
            pass  # TestClient re-raises; a real deployment returns 500 and Stripe retries

    assert db_session.scalar(
        select(CheckoutSession).where(CheckoutSession.stripe_pi_id == pi_id)
    ) is not None, "the session was consumed, so the retry can never void the auth"


def test_webhook_still_creates_the_order_when_stock_is_there(client, db_session):
    """The guard must not have changed the ordinary path, and it takes stock."""
    variant = make_product_and_variant(db_session)
    pi_id = f"pi_{uuid.uuid4().hex}"
    make_checkout_session(db_session, pi_id, variant.id)

    response = post_webhook(client, pi_id, "payment_intent.amount_capturable_updated")

    assert response.status_code == 200
    order = db_session.scalar(select(Order).where(Order.stripe_payment_intent_id == pi_id))
    assert order is not None
    assert order.status == OrderStatus.awaiting_fulfillment

    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock == 4, (
        "creating the order did not take the stock"
    )
