import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

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


def test_webhook_payment_succeeded_creates_order(client, db_session):
    """payment_intent.succeeded creates an Order from the CheckoutSession."""
    pi_id = f"pi_{uuid.uuid4().hex}"
    variant = make_product_and_variant(db_session)
    make_checkout_session(db_session, pi_id, variant.id)

    response = post_webhook(client, pi_id, "payment_intent.succeeded")

    assert response.status_code == 200
    order = db_session.scalar(
        __import__("sqlalchemy", fromlist=["select"]).select(Order).where(Order.stripe_payment_intent_id == pi_id)
    )
    assert order is not None
    assert order.status == OrderStatus.awaiting_fulfillment
    # Session must be cleaned up
    session = db_session.scalar(
        __import__("sqlalchemy", fromlist=["select"]).select(CheckoutSession).where(CheckoutSession.stripe_pi_id == pi_id)
    )
    assert session is None


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


def test_webhook_payment_succeeded_no_session_is_noop(client, db_session):
    """Duplicate or late payment_intent.succeeded with no session is a safe no-op."""
    pi_id = f"pi_{uuid.uuid4().hex}"

    response = post_webhook(client, pi_id, "payment_intent.succeeded")

    assert response.status_code == 200


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
    pi_id = f"pi_{uuid.uuid4().hex}"
    variant = make_product_and_variant(db_session)
    make_checkout_session(db_session, pi_id, variant.id)

    # First attempt: card declined — session still intact
    post_webhook(client, pi_id, "payment_intent.payment_failed")

    # Second attempt: good card — order created
    response = post_webhook(client, pi_id, "payment_intent.succeeded")

    assert response.status_code == 200
    from sqlalchemy import select
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
