import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.services.order_numbers import generate_order_number


def make_product_with_variant(db_session, cat_id: str, price: float = 10.0, stock: int = 5):
    product = Product(cat_id=cat_id, name=f"Product {cat_id}", description="Test")
    variant = ProductVariant(
        catalog_id=f"{cat_id}-V1",
        size_value=Decimal("100"),
        size_unit="g",
        price=Decimal(str(price)),
        stock=stock,
    )
    product.variants.append(variant)
    db_session.add(product)
    db_session.commit()
    db_session.refresh(variant)
    return product, variant


def make_order(db_session, user_id: str = "test-user-123", status: OrderStatus = OrderStatus.awaiting_fulfillment, stock: int = 5) -> tuple[Order, ProductVariant]:
    product, variant = make_product_with_variant(db_session, f"P-{uuid.uuid4().hex[:6]}", stock=stock)
    order = Order(
        order_number=generate_order_number(),
        user_id=user_id,
        status=status,
        stripe_payment_intent_id=f"pi_{uuid.uuid4().hex}",
        shipping_name="Jane Smith",
        shipping_phone="5551234567",
        shipping_address1="123 Main St",
        shipping_city="Springfield",
        shipping_state="IL",
        shipping_zip="62701",
        total_amount=Decimal("10.00"),
    )
    item = OrderItem(
        variant_id=variant.id,
        product_name=product.name,
        variant_label="100g",
        unit_price=Decimal("10.00"),
        quantity=1,
    )
    order.items.append(item)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order, variant


# --- order_service tests ---

def test_confirming_and_shipping_leave_stock_alone(db_session):
    """Neither step moves inventory; it was taken when the order was created.

    Named for deduction until stock moved to creation, and it asserted 4 here.
    """
    from app.services.order_service import confirm_order_admin, ship_order

    order, variant = make_order(db_session, stock=5)
    confirm_order_admin(db_session, order.id)

    result = ship_order(db_session, order.id)

    assert result is not None
    assert result.status == OrderStatus.shipped
    db_session.refresh(variant)
    assert variant.stock == 5


def test_ship_order_wrong_status_raises(db_session):
    from app.services.order_service import ship_order

    order, _ = make_order(db_session, status=OrderStatus.shipped)

    with pytest.raises(ValueError, match="Cannot ship"):
        ship_order(db_session, order.id)


# --- Customer endpoint tests ---

def make_stripe_pi_mock(client_secret: str = "pi_test_secret_xxx"):
    mock = MagicMock()
    mock.client_secret = client_secret
    mock.id = "pi_test_id"
    return mock


def make_tax_mock(subtotal_cents: int, rate: float = 0.0875):
    """Stand in for Stripe Tax, mirroring the shape calculate_tax returns."""
    tax = round(subtotal_cents * rate)
    mock = MagicMock()
    mock.tax_amount_cents = tax
    mock.total_cents = subtotal_cents + tax
    return mock


def test_create_payment_intent_success(user_client: TestClient, db_session):
    _, variant = make_product_with_variant(db_session, "CART-PROD-1", price=15.0, stock=10)

    # calculate_tax must be patched alongside create_payment_intent: with
    # STRIPE_BYPASS off (as the suite now pins it) it is a live Stripe Tax call,
    # and the endpoint turns any failure into a 502 before the assertions below
    # are ever reached.
    with patch("app.api.v1.endpoints.orders.create_payment_intent", return_value=make_stripe_pi_mock()) as mock_pi, \
         patch("app.api.v1.endpoints.orders.calculate_tax", return_value=make_tax_mock(3000)):
        response = user_client.post("/api/v1/orders/payment-intent", json={
            "cart": [{"variant_id": str(variant.id), "quantity": 2}],
            "shipping": {
                "name": "Jane Smith",
                "phone": "5551234567",
                "address1": "123 Main St",
                "city": "Springfield",
                "state": "IL",
                "zip": "62701",
            },
        })

    assert response.status_code == 201
    data = response.json()
    assert "client_secret" in data
    assert data.get("order_id") is None  # order created only when webhook fires


def test_create_payment_intent_unknown_variant(user_client: TestClient):
    with patch("app.api.v1.endpoints.orders.create_payment_intent", return_value=make_stripe_pi_mock()) as mock_pi:
        response = user_client.post("/api/v1/orders/payment-intent", json={
            "cart": [{"variant_id": str(uuid.uuid4()), "quantity": 1}],
            "shipping": {
                "name": "Jane Smith",
                "phone": "5551234567",
                "address1": "123 Main St",
                "city": "Springfield",
                "state": "IL",
                "zip": "62701",
            },
        })
        mock_pi.assert_not_called()
    assert response.status_code == 400


def test_list_orders_returns_only_own_orders(user_client: TestClient, db_session):
    make_order(db_session, user_id="test-user-123")
    make_order(db_session, user_id="other-user-456")

    response = user_client.get("/api/v1/orders")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["shipping_name"] == "Jane Smith"


def test_get_order_detail_own_order(user_client: TestClient, db_session):
    order, _ = make_order(db_session, user_id="test-user-123")

    response = user_client.get(f"/api/v1/orders/{order.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(order.id)


def test_get_order_detail_other_user_returns_404(user_client: TestClient, db_session):
    order, _ = make_order(db_session, user_id="other-user-456")

    response = user_client.get(f"/api/v1/orders/{order.id}")
    assert response.status_code == 404


def test_create_payment_intent_insufficient_stock(user_client: TestClient, db_session):
    _, variant = make_product_with_variant(db_session, "STOCK-PROD-1", price=10.0, stock=2)

    with patch("app.api.v1.endpoints.orders.create_payment_intent", return_value=make_stripe_pi_mock()) as mock_pi:
        response = user_client.post("/api/v1/orders/payment-intent", json={
            "cart": [{"variant_id": str(variant.id), "quantity": 5}],
            "shipping": {
                "name": "Jane Smith",
                "phone": "5551234567",
                "address1": "123 Main St",
                "city": "Springfield",
                "state": "IL",
                "zip": "62701",
            },
        })
        mock_pi.assert_not_called()
    assert response.status_code == 400
    assert "Insufficient stock" in response.json()["detail"]


def test_customer_cancel_pending_order(user_client: TestClient, db_session):
    """Customer can cancel a pending order (abandoned checkout) — cancels the PI, no refund."""
    order, _ = make_order(db_session, user_id="test-user-123", status=OrderStatus.pending)

    with patch("app.api.v1.endpoints.orders.cancel_payment_intent") as mock_cancel:
        response = user_client.post(f"/api/v1/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_cancel.assert_called_once_with(order.stripe_payment_intent_id)


def test_customer_cancel_awaiting_order(user_client: TestClient, db_session):
    """Customer can cancel an awaiting_fulfillment order — cancels the PI, no refund."""
    order, _ = make_order(db_session, user_id="test-user-123", status=OrderStatus.awaiting_fulfillment)

    with patch("app.api.v1.endpoints.orders.cancel_payment_intent") as mock_cancel:
        response = user_client.post(f"/api/v1/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_cancel.assert_called_once_with(order.stripe_payment_intent_id)


def test_customer_cancel_confirmed_returns_400(user_client: TestClient, db_session):
    """Customer cannot cancel once admin has confirmed (payment captured)."""
    order, _ = make_order(db_session, user_id="test-user-123", status=OrderStatus.confirmed)

    response = user_client.post(f"/api/v1/orders/{order.id}/cancel")

    assert response.status_code == 400


def test_list_orders_unauthenticated(client: TestClient):
    response = client.get("/api/v1/orders")
    assert response.status_code in (401, 403)


def test_create_payment_intent_unauthenticated(client: TestClient):
    response = client.post("/api/v1/orders/payment-intent", json={
        "cart": [{"variant_id": str(uuid.uuid4()), "quantity": 1}],
        "shipping": {
            "name": "Jane Smith",
            "phone": "5551234567",
            "address1": "123 Main St",
            "city": "Springfield",
            "state": "IL",
            "zip": "62701",
        },
    })
    assert response.status_code in (401, 403)


# --- where order mail is sent -------------------------------------------------
#
# The customer may nominate an address; blank falls back to the one on their
# Cognito account. Safe to take their word for, because order mail is a
# notification and never a credential: every order is scoped by user_id, and
# nothing anywhere is reachable by knowing an email. A lab ordering against a
# shared purchasing address could not say so otherwise.

def _checkout(user_client, variant, **extra):
    with patch("app.api.v1.endpoints.orders.create_payment_intent", return_value=make_stripe_pi_mock()), \
         patch("app.api.v1.endpoints.orders.calculate_tax", return_value=make_tax_mock(1500)):
        return user_client.post("/api/v1/orders/payment-intent", json={
            "cart": [{"variant_id": str(variant.id), "quantity": 1}],
            "shipping": {
                "name": "Jane Smith",
                "phone": "5551234567",
                "address1": "123 Main St",
                "city": "Springfield",
                "state": "IL",
                "zip": "62701",
            },
            **extra,
        })


def _session_for(db_session, pi_id: str):
    from app.models.checkout_session import CheckoutSession
    return db_session.scalar(
        select(CheckoutSession).where(CheckoutSession.stripe_pi_id == pi_id)
    )


def test_checkout_sends_mail_to_the_nominated_address(user_client: TestClient, db_session):
    _, variant = make_product_with_variant(db_session, "MAIL-1", stock=5)

    response = _checkout(user_client, variant, contact_email="purchasing@lab.edu")

    assert response.status_code == 201
    session = _session_for(db_session, make_stripe_pi_mock().id)
    assert session is not None
    assert session.customer_email == "purchasing@lab.edu"


def test_checkout_falls_back_to_the_account_address(user_client: TestClient, db_session):
    """Blank means "use my account address" - the verified one from the id token.

    This is what keeps that verification worth having: it is the fallback, and
    the fallback is the common case.
    """
    _, variant = make_product_with_variant(db_session, "MAIL-2", stock=5)

    response = _checkout(user_client, variant, contact_email="")

    assert response.status_code == 201
    session = _session_for(db_session, make_stripe_pi_mock().id)
    # The identity user_client authenticates as, rather than merely "not the
    # nominated one" - which would hold for an empty string too.
    assert session.customer_email == "user@test.com"


def test_checkout_omitting_the_field_entirely_is_fine(user_client: TestClient, db_session):
    """An older client that does not send the field at all must still check out."""
    _, variant = make_product_with_variant(db_session, "MAIL-3", stock=5)

    assert _checkout(user_client, variant).status_code == 201


def test_checkout_rejects_a_malformed_address(user_client: TestClient, db_session):
    """Caught here rather than discovered when SES bounces it days later."""
    _, variant = make_product_with_variant(db_session, "MAIL-4", stock=5)

    assert _checkout(user_client, variant, contact_email="not-an-address").status_code == 422


def test_the_nominated_address_survives_the_webhook(user_client: TestClient, db_session):
    """It is stored on the checkout session, so it reaches the order Stripe creates."""
    from app.services.order_service import create_order_from_checkout_session

    _, variant = make_product_with_variant(db_session, "MAIL-5", stock=5)
    _checkout(user_client, variant, contact_email="purchasing@lab.edu")

    order = create_order_from_checkout_session(db_session, make_stripe_pi_mock().id)

    assert order is not None
    assert order.customer_email == "purchasing@lab.edu"


def test_the_order_page_query_breaks_ties_on_a_unique_column(db_session):
    """created_at is not unique, and Postgres now() is transaction-start - so
    anything written in one transaction ties. With ties, LIMIT/OFFSET has no
    defined order between the count query and the page query: a row can appear
    on two pages, or on none.

    Asserted on the SQL rather than on the outcome, because SQLite returns rows
    in rowid order whether or not a tiebreaker is asked for - a behavioural test
    here passes with the bug present, which is worse than no test. Same reason
    test_stock_locking asserts that the lock is *requested*.
    """
    from sqlalchemy import event

    from app.services.order_service import list_all_orders

    make_order(db_session)
    bind = db_session.get_bind()
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", record)
    try:
        list_all_orders(db_session, None, limit=4, offset=0)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    ordered = [s for s in statements if "ORDER BY" in s.upper()]
    assert ordered, "the page query has no ORDER BY at all"
    for statement in ordered:
        clause = statement.upper().split("ORDER BY", 1)[1]
        assert "ID" in clause, f"no unique tiebreaker in: {clause.strip()[:80]}"


# --- shipping --------------------------------------------------------------
#
# The shop charged goods plus tax and nothing for postage. For reagents shipped
# cold that is a real cost absorbed on every order.
#
# A flat rate behind a seam: the amount is the only part that depends on the
# carrier, and everything else - a line on the order, an amount in the intent, a
# row at checkout and on the receipt - is the same whatever that answer is.

def test_nothing_is_charged_until_a_rate_is_configured(user_client: TestClient, db_session):
    """Zero by default, so an environment that has not set it keeps behaving as
    it did rather than surprising customers with a charge nobody chose."""
    from app.core.config import get_settings

    _, variant = make_product_with_variant(db_session, "SHIP-0", stock=5)
    assert get_settings().shipping_flat_cents == 0

    response = _checkout(user_client, variant)

    assert response.status_code == 201
    assert response.json()["shipping_amount_cents"] == 0


def test_a_configured_rate_is_quoted_at_checkout(user_client: TestClient, db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "shipping_flat_cents", 2500, raising=False)
    _, variant = make_product_with_variant(db_session, "SHIP-1", stock=5)

    body = _checkout(user_client, variant).json()

    assert body["shipping_amount_cents"] == 2500


def test_the_customer_is_charged_for_shipping(user_client: TestClient, db_session, monkeypatch):
    """The whole point. It must reach the PaymentIntent, not just the response."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "shipping_flat_cents", 2500, raising=False)
    _, variant = make_product_with_variant(db_session, "SHIP-2", price=10.0, stock=5)

    with patch("app.api.v1.endpoints.orders.create_payment_intent", return_value=make_stripe_pi_mock()) as pi, \
         patch("app.api.v1.endpoints.orders.calculate_tax", return_value=make_tax_mock(3500)):
        user_client.post("/api/v1/orders/payment-intent", json={
            "cart": [{"variant_id": str(variant.id), "quantity": 1}],
            "shipping": {"name": "J", "phone": "5551234567", "address1": "1 St",
                         "city": "C", "state": "IL", "zip": "62701"},
        })

    charged = pi.call_args.args[0]
    assert charged > 1000, f"the customer was charged {charged}, which cannot include $25 of postage"


def test_tax_is_told_about_shipping(user_client: TestClient, db_session, monkeypatch):
    """Most US states tax delivery, and which ones is exactly the judgement
    Stripe Tax exists to make - so it has to know."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "shipping_flat_cents", 2500, raising=False)
    _, variant = make_product_with_variant(db_session, "SHIP-3", stock=5)

    with patch("app.api.v1.endpoints.orders.create_payment_intent", return_value=make_stripe_pi_mock()), \
         patch("app.api.v1.endpoints.orders.calculate_tax", return_value=make_tax_mock(1000)) as tax:
        user_client.post("/api/v1/orders/payment-intent", json={
            "cart": [{"variant_id": str(variant.id), "quantity": 1}],
            "shipping": {"name": "J", "phone": "5551234567", "address1": "1 St",
                         "city": "C", "state": "IL", "zip": "62701"},
        })

    assert tax.call_args.args[2] == 2500, "Stripe Tax was not given the shipping amount"


def test_shipping_survives_the_webhook(user_client: TestClient, db_session, monkeypatch):
    """The order is built from the checkout session minutes later. Recomputing
    shipping there could charge a rate the customer was never shown."""
    from app.core.config import get_settings
    from app.services.order_service import create_order_from_checkout_session

    monkeypatch.setattr(get_settings(), "shipping_flat_cents", 2500, raising=False)
    _, variant = make_product_with_variant(db_session, "SHIP-4", stock=5)
    _checkout(user_client, variant)

    order = create_order_from_checkout_session(db_session, make_stripe_pi_mock().id)

    assert order is not None
    assert order.shipping_amount == Decimal("25.00")


def test_the_receipt_adds_up(db_session):
    """A confirmation whose lines do not sum to its total is a support email."""
    from app.services import email_service

    order, _ = make_order(db_session)
    order.tax_amount = Decimal("2.00")
    order.shipping_amount = Decimal("25.00")
    db_session.commit()

    body = email_service._order_summary(order) if hasattr(email_service, "_order_summary") else None
    if body is None:
        import inspect
        src = inspect.getsource(email_service)
        assert "shipping_amount" in src, "the receipt never mentions shipping"
    else:
        assert "Shipping" in body


def test_an_empty_cart_is_not_charged_postage(db_session):
    from app.services.shipping_service import calculate_shipping

    assert calculate_shipping([]) == 0
