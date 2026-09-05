import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

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

def test_ship_order_deducts_stock(db_session):
    from app.services.order_service import confirm_order_admin, ship_order

    order, variant = make_order(db_session, stock=5)
    confirm_order_admin(db_session, order.id)

    result = ship_order(db_session, order.id)

    assert result is not None
    assert result.status == OrderStatus.shipped
    db_session.refresh(variant)
    assert variant.stock == 4


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
