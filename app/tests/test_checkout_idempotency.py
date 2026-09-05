"""A retried checkout must not open a second authorization hold.

POST /orders/payment-intent creates a Stripe PaymentIntent, which places a hold
on the customer's card. Nothing about that request is naturally idempotent, and
it is trivially retried: a double-clicked Pay button, a flaky connection, a
browser replaying a request. Without an idempotency key each retry produces
another intent - two holds against one basket, and only ever one of them gets
voided or captured. The customer sees their available balance reduced twice.
"""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.schemas.order import CartItemIn, ShippingIn
from app.services.stripe_service import TaxResult
from app.tests.test_orders import make_product_with_variant

SHIPPING = {
    "name": "Jane Smith",
    "phone": "5551234567",
    "address1": "123 Main St",
    "address2": None,
    "city": "Springfield",
    "state": "IL",
    "zip": "62701",
    "notes": None,
}


def _post(user_client: TestClient, variant_id, quantity: int = 1, shipping: dict | None = None):
    """Run checkout with Stripe stubbed, returning the create_payment_intent spy."""
    pi = MagicMock()
    pi.id = f"pi_{uuid.uuid4().hex}"
    pi.client_secret = "cs_test"
    create_pi = MagicMock(return_value=pi)

    with patch("app.api.v1.endpoints.orders.create_payment_intent", create_pi):
        with patch(
            "app.api.v1.endpoints.orders.calculate_tax",
            MagicMock(return_value=TaxResult(tax_amount_cents=88, total_cents=1088)),
        ):
            response = user_client.post(
                "/api/v1/orders/payment-intent",
                json={
                    "cart": [{"variant_id": str(variant_id), "quantity": quantity}],
                    "shipping": shipping or SHIPPING,
                },
            )
    return response, create_pi


def _key_of(spy) -> str:
    return spy.call_args.kwargs["idempotency_key"]


def test_an_idempotency_key_is_sent(user_client: TestClient, db_session):
    _, variant = make_product_with_variant(db_session, "IDEM-01")

    response, spy = _post(user_client, variant.id)

    assert response.status_code == 201
    assert _key_of(spy), "no idempotency key reached Stripe"


def test_the_same_basket_produces_the_same_key(user_client: TestClient, db_session):
    """The retry case: identical request, so Stripe returns the first intent
    instead of opening a second hold."""
    _, variant = make_product_with_variant(db_session, "IDEM-02", stock=10)

    _, first = _post(user_client, variant.id)
    _, second = _post(user_client, variant.id)

    assert _key_of(first) == _key_of(second)


def test_a_changed_quantity_produces_a_different_key(user_client: TestClient, db_session):
    """Genuinely re-submitting an edited basket is a different charge and must
    get its own intent, not Stripe's cached reply for the old amount."""
    _, variant = make_product_with_variant(db_session, "IDEM-03", stock=10)

    _, first = _post(user_client, variant.id, quantity=1)
    _, second = _post(user_client, variant.id, quantity=2)

    assert _key_of(first) != _key_of(second)


def test_a_changed_address_produces_a_different_key(user_client: TestClient, db_session):
    _, variant = make_product_with_variant(db_session, "IDEM-04", stock=10)

    _, first = _post(user_client, variant.id)
    _, second = _post(user_client, variant.id, shipping={**SHIPPING, "zip": "10001"})

    assert _key_of(first) != _key_of(second)


def test_the_key_is_within_stripes_length_limit(user_client: TestClient, db_session):
    """Stripe caps idempotency keys at 255 characters."""
    _, variant = make_product_with_variant(db_session, "IDEM-05")

    _, spy = _post(user_client, variant.id)

    assert 0 < len(_key_of(spy)) <= 255


def test_the_key_does_not_leak_the_customer_identity(user_client: TestClient, db_session):
    """It is derived from the user's sub, but hashed - the value travels to a
    third party and reaches their dashboard and logs."""
    _, variant = make_product_with_variant(db_session, "IDEM-06")

    _, spy = _post(user_client, variant.id)

    assert "test-user-123" not in _key_of(spy)


# --- the other half of the idempotency key ---
#
# The key stops Stripe opening a second PaymentIntent for a resubmitted
# checkout - which means the *same* payment intent id then arrives at
# save_checkout_session, where stripe_pi_id is unique. Inserting blindly turned
# the retry the key had just made safe into a 500. Found by resubmitting an
# identical checkout against the real Stripe API, not by a unit test.

def test_resubmitting_a_checkout_does_not_500(user_client: TestClient, db_session):
    _, variant = make_product_with_variant(db_session, "IDEM-07", stock=10)

    pi = MagicMock()
    pi.id = "pi_resubmitted_same_intent"
    pi.client_secret = "cs_test"

    def submit():
        with patch("app.api.v1.endpoints.orders.create_payment_intent", MagicMock(return_value=pi)):
            with patch(
                "app.api.v1.endpoints.orders.calculate_tax",
                MagicMock(return_value=TaxResult(tax_amount_cents=88, total_cents=1088)),
            ):
                return user_client.post(
                    "/api/v1/orders/payment-intent",
                    json={
                        "cart": [{"variant_id": str(variant.id), "quantity": 1}],
                        "shipping": SHIPPING,
                    },
                )

    assert submit().status_code == 201
    assert submit().status_code == 201


def test_a_resubmitted_checkout_leaves_exactly_one_session(user_client: TestClient, db_session):
    """Two sessions for one payment intent would mean the webhook converts one
    and leaves the other to be swept eight days later."""
    from app.models.checkout_session import CheckoutSession

    _, variant = make_product_with_variant(db_session, "IDEM-08", stock=10)
    pi = MagicMock()
    pi.id = "pi_only_one_session"
    pi.client_secret = "cs_test"

    for _ in range(3):
        with patch("app.api.v1.endpoints.orders.create_payment_intent", MagicMock(return_value=pi)):
            with patch(
                "app.api.v1.endpoints.orders.calculate_tax",
                MagicMock(return_value=TaxResult(tax_amount_cents=88, total_cents=1088)),
            ):
                user_client.post(
                    "/api/v1/orders/payment-intent",
                    json={
                        "cart": [{"variant_id": str(variant.id), "quantity": 1}],
                        "shipping": SHIPPING,
                    },
                )

    sessions = db_session.query(CheckoutSession).filter_by(stripe_pi_id="pi_only_one_session").all()
    assert len(sessions) == 1


def test_a_resubmitted_checkout_stores_the_latest_details(db_session):
    """The webhook builds the order from whatever is stored here, so a corrected
    address on the second attempt has to be the one that survives."""
    from app.services.order_service import save_checkout_session

    _, variant = make_product_with_variant(db_session, "IDEM-09", stock=10)
    cart = [CartItemIn(variant_id=variant.id, quantity=1)]

    first = ShippingIn(**{**SHIPPING, "address1": "1 Wrong St"})
    corrected = ShippingIn(**{**SHIPPING, "address1": "2 Right Ave"})

    save_checkout_session(db_session, "pi_same", "u", cart, first, 88, "a@example.com")
    session = save_checkout_session(db_session, "pi_same", "u", cart, corrected, 99, "b@example.com")

    assert "2 Right Ave" in session.shipping_json
    assert session.tax_amount_cents == 99
    assert session.customer_email == "b@example.com"
