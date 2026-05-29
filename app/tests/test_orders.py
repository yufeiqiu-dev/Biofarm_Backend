import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant


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
        order_number=1000 + db_session.query(Order).count(),
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

def test_confirm_order_transitions_to_awaiting_fulfillment(db_session):
    from app.services.order_service import confirm_order

    order, _ = make_order(db_session, status=OrderStatus.pending)
    pi_id = order.stripe_payment_intent_id

    result = confirm_order(db_session, pi_id)

    assert result is not None
    assert result.status == OrderStatus.awaiting_fulfillment


def test_confirm_order_unknown_pi_returns_none(db_session):
    from app.services.order_service import confirm_order

    result = confirm_order(db_session, "pi_nonexistent")
    assert result is None


def test_ship_order_deducts_stock(db_session):
    from app.services.order_service import ship_order

    order, variant = make_order(db_session, stock=5)
    variant_id = variant.id

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


def test_cancel_order_from_failed_payment(db_session):
    from app.services.order_service import cancel_order_from_failed_payment

    order, _ = make_order(db_session, status=OrderStatus.pending)
    pi_id = order.stripe_payment_intent_id

    result = cancel_order_from_failed_payment(db_session, pi_id)

    assert result is not None
    assert result.status == OrderStatus.cancelled


# --- Customer endpoint tests ---

def make_stripe_pi_mock(client_secret: str = "pi_test_secret_xxx"):
    mock = MagicMock()
    mock.client_secret = client_secret
    mock.id = "pi_test_id"
    return mock


def test_create_payment_intent_success(user_client: TestClient, db_session):
    _, variant = make_product_with_variant(db_session, "CART-PROD-1", price=15.0, stock=10)

    with patch("app.api.v1.endpoints.orders.create_payment_intent", return_value=make_stripe_pi_mock()) as mock_pi:
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
    assert "order_id" in data


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
