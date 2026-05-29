import uuid
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant


def make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment, qty=1, stock=5):
    product = Product(cat_id=f"ADM-{uuid.uuid4().hex[:4]}", name="Admin Test", description="Test")
    variant = ProductVariant(
        catalog_id=f"ADM-V-{uuid.uuid4().hex[:4]}",
        size_value=Decimal("100"),
        size_unit="g",
        price=Decimal("20.00"),
        stock=stock,
    )
    product.variants.append(variant)
    db_session.add(product)

    order = Order(
        order_number=3000 + db_session.query(Order).count(),
        user_id="customer-sub-abc",
        status=status,
        stripe_payment_intent_id=f"pi_{uuid.uuid4().hex}",
        shipping_name="Customer Name",
        shipping_phone="5559876543",
        shipping_address1="456 Elm St",
        shipping_city="Portland",
        shipping_state="OR",
        shipping_zip="97201",
        total_amount=Decimal("20.00"),
    )
    item = OrderItem(
        variant_id=variant.id,
        product_name="Admin Test",
        variant_label="100g",
        unit_price=Decimal("20.00"),
        quantity=qty,
    )
    order.items.append(item)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    db_session.refresh(variant)
    return order, variant


def test_admin_list_orders(admin_client: TestClient, db_session):
    make_order_for_admin(db_session)
    make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.get("/api/v1/admin/orders")
    assert response.status_code == 200
    assert len(response.json()) >= 2


def test_admin_list_orders_status_filter(admin_client: TestClient, db_session):
    make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.get("/api/v1/admin/orders?status=shipped")
    assert response.status_code == 200
    data = response.json()
    assert all(o["status"] == "shipped" for o in data)


def test_admin_get_order_detail_includes_stock(admin_client: TestClient, db_session):
    order, variant = make_order_for_admin(db_session, qty=2, stock=3)

    response = admin_client.get(f"/api/v1/admin/orders/{order.id}")
    assert response.status_code == 200
    data = response.json()
    item = data["items"][0]
    assert item["current_stock"] == 3
    assert item["quantity"] == 2


def test_admin_ship_order_deducts_stock(admin_client: TestClient, db_session):
    order, variant = make_order_for_admin(db_session, qty=1, stock=5)

    response = admin_client.patch(
        f"/api/v1/admin/orders/{order.id}/status",
        json={"status": "shipped"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "shipped"

    db_session.refresh(variant)
    assert variant.stock == 4


def test_admin_cancel_order_issues_refund(admin_client: TestClient, db_session):
    order, _ = make_order_for_admin(db_session)

    mock_refund = object()
    with patch("app.api.v1.endpoints.admin_orders.create_refund", return_value=mock_refund):
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_admin_ship_wrong_status_returns_400(admin_client: TestClient, db_session):
    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.patch(
        f"/api/v1/admin/orders/{order.id}/status",
        json={"status": "shipped"},
    )
    assert response.status_code == 400
