import uuid
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.services.order_numbers import generate_order_number


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
    # Explicit, because the OrderItem below needs variant.id. This used to work
    # only because the order_number expression ran a query, whose autoflush
    # assigned the id as a side effect - so changing an unrelated line silently
    # left variant_id as None and the stock assertions stopped meaning anything.
    db_session.flush()

    order = Order(
        order_number=generate_order_number(),
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


def test_admin_confirm_order_does_not_capture(admin_client: TestClient, db_session):
    """Confirm just marks the order — no payment capture until ship."""
    order, _ = make_order_for_admin(db_session)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent") as mock_capture:
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    mock_capture.assert_not_called()


def test_admin_confirm_order_deducts_stock(admin_client: TestClient, db_session):
    """Confirming deducts stock — admin is setting aside inventory."""
    order, variant = make_order_for_admin(db_session, qty=2, stock=5)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent"):
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )
    assert response.status_code == 200
    db_session.refresh(variant)
    assert variant.stock == 3


def test_admin_confirm_order_insufficient_stock_returns_400(admin_client: TestClient, db_session):
    """Confirming an order with insufficient stock is blocked."""
    order, variant = make_order_for_admin(db_session, qty=3, stock=2)

    response = admin_client.patch(
        f"/api/v1/admin/orders/{order.id}/status",
        json={"status": "confirmed"},
    )
    assert response.status_code == 400
    db_session.refresh(variant)
    assert variant.stock == 2  # unchanged


def test_admin_ship_order_captures_no_stock_change(admin_client: TestClient, db_session):
    """Shipping captures payment; stock was already deducted at confirm."""
    order, variant = make_order_for_admin(db_session, status=OrderStatus.confirmed, qty=1, stock=4)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent") as mock_capture:
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "shipped"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "shipped"
    mock_capture.assert_called_once_with(order.stripe_payment_intent_id)

    db_session.refresh(variant)
    assert variant.stock == 4  # unchanged — deducted at confirm, not ship


def test_admin_cancel_confirmed_restores_stock(admin_client: TestClient, db_session):
    """Cancelling a confirmed order restores stock."""
    order, variant = make_order_for_admin(db_session, status=OrderStatus.confirmed, qty=2, stock=3)

    with patch("app.api.v1.endpoints.admin_orders.cancel_payment_intent"):
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    db_session.refresh(variant)
    assert variant.stock == 5  # 3 + 2 restored


def test_admin_cancel_pending_cancels_auth(admin_client: TestClient, db_session):
    """Cancelling a pending order (abandoned checkout) cancels the PI — no refund."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.pending)

    with patch("app.api.v1.endpoints.admin_orders.cancel_payment_intent") as mock_cancel, \
         patch("app.api.v1.endpoints.admin_orders.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_cancel.assert_called_once_with(order.stripe_payment_intent_id)
    mock_refund.assert_not_called()


def test_admin_cancel_awaiting_cancels_auth(admin_client: TestClient, db_session):
    """Cancelling an awaiting_fulfillment order cancels the auth — no refund."""
    order, _ = make_order_for_admin(db_session)

    with patch("app.api.v1.endpoints.admin_orders.cancel_payment_intent") as mock_cancel, \
         patch("app.api.v1.endpoints.admin_orders.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_cancel.assert_called_once_with(order.stripe_payment_intent_id)
    mock_refund.assert_not_called()


def test_admin_cancel_confirmed_voids_auth(admin_client: TestClient, db_session):
    """Cancelling a confirmed order voids the auth — payment not yet captured (capture happens at ship)."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.confirmed)

    with patch("app.api.v1.endpoints.admin_orders.cancel_payment_intent") as mock_cancel, \
         patch("app.api.v1.endpoints.admin_orders.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_cancel.assert_called_once_with(order.stripe_payment_intent_id)
    mock_refund.assert_not_called()


def test_admin_cancel_shipped_issues_refund(admin_client: TestClient, db_session):
    """Cancelling a shipped order issues a refund — money was already captured."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped)

    with patch("app.api.v1.endpoints.admin_orders.create_refund") as mock_refund, \
         patch("app.api.v1.endpoints.admin_orders.cancel_payment_intent") as mock_cancel:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_refund.assert_called_once_with(order.stripe_payment_intent_id)
    mock_cancel.assert_not_called()


def test_admin_cancel_delivered_issues_refund(admin_client: TestClient, db_session):
    """Cancelling a delivered order issues a refund — payment was captured at ship."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.delivered)

    with patch("app.api.v1.endpoints.admin_orders.create_refund") as mock_refund, \
         patch("app.api.v1.endpoints.admin_orders.cancel_payment_intent") as mock_cancel:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_refund.assert_called_once_with(order.stripe_payment_intent_id)
    mock_cancel.assert_not_called()


def test_admin_ship_wrong_status_returns_400(admin_client: TestClient, db_session):
    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.patch(
        f"/api/v1/admin/orders/{order.id}/status",
        json={"status": "shipped"},
    )
    assert response.status_code == 400


def test_admin_cancel_already_cancelled_returns_400(admin_client: TestClient, db_session):
    """Cancel on already-cancelled order must not call any Stripe operation."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.cancelled)

    with patch("app.api.v1.endpoints.admin_orders.cancel_payment_intent") as mock_cancel, \
         patch("app.api.v1.endpoints.admin_orders.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 400
    mock_cancel.assert_not_called()
    mock_refund.assert_not_called()


def test_admin_deliver_order(admin_client: TestClient, db_session):
    """PATCH status=delivered transitions shipped → delivered."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.patch(
        f"/api/v1/admin/orders/{order.id}/status",
        json={"status": "delivered"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"


def test_admin_ship_nonexistent_order_returns_404(admin_client: TestClient):
    response = admin_client.patch(
        f"/api/v1/admin/orders/{uuid.uuid4()}/status",
        json={"status": "shipped"},
    )
    assert response.status_code == 404


# --- the status query parameter ---

def test_status_filter_still_answers_to_the_status_query_parameter(admin_client, db_session):
    """The handler's parameter was renamed to status_filter because `status`
    shadowed the fastapi status module - which is why this one function had to
    hardcode 400 where every sibling uses the constant. The URL contract is
    unchanged, and this is what proves it."""
    from app.models.order import OrderStatus
    from app.tests.test_orders import make_order

    make_order(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order(db_session, status=OrderStatus.delivered)

    response = admin_client.get("/api/v1/admin/orders?status=delivered")

    assert response.status_code == 200
    assert [o["status"] for o in response.json()] == ["delivered"]


def test_an_unknown_status_is_a_400_from_the_shared_constant(admin_client):
    response = admin_client.get("/api/v1/admin/orders?status=not-a-status")

    assert response.status_code == 400
    assert "not-a-status" in response.json()["detail"]


def test_no_status_returns_every_order(admin_client, db_session):
    from app.models.order import OrderStatus
    from app.tests.test_orders import make_order

    make_order(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order(db_session, status=OrderStatus.delivered)

    response = admin_client.get("/api/v1/admin/orders")

    assert response.status_code == 200
    assert len(response.json()) == 2
