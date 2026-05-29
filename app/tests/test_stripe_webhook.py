import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.models.order import Order, OrderItem, OrderStatus


def make_pending_order(db_session, pi_id: str = "pi_test_123") -> Order:
    from app.models.product import Product
    from app.models.product_variant import ProductVariant

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

    order = Order(
        order_number=2000 + db_session.query(Order).count(),
        user_id="wh-user",
        status=OrderStatus.pending,
        stripe_payment_intent_id=pi_id,
        shipping_name="Test",
        shipping_phone="5550000000",
        shipping_address1="1 Test St",
        shipping_city="TestCity",
        shipping_state="CA",
        shipping_zip="90210",
        total_amount=Decimal("10.00"),
    )
    item = OrderItem(
        variant_id=variant.id,
        product_name="Test",
        variant_label="100g",
        unit_price=Decimal("10.00"),
        quantity=1,
    )
    order.items.append(item)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def make_stripe_event(event_type: str, pi_id: str) -> MagicMock:
    event = MagicMock()
    event.type = event_type
    event.data.object.id = pi_id
    return event


def test_webhook_payment_succeeded_confirms_order(client, db_session):
    pi_id = f"pi_{uuid.uuid4().hex}"
    order = make_pending_order(db_session, pi_id)

    with patch("app.api.v1.endpoints.stripe_webhook.verify_webhook_signature",
               return_value=make_stripe_event("payment_intent.succeeded", pi_id)):
        response = client.post(
            "/api/v1/stripe/webhook",
            content=b"fake-payload",
            headers={"stripe-signature": "t=1,v1=fake"},
        )

    assert response.status_code == 200
    db_session.refresh(order)
    assert order.status == OrderStatus.awaiting_fulfillment


def test_webhook_payment_failed_cancels_order(client, db_session):
    pi_id = f"pi_{uuid.uuid4().hex}"
    order = make_pending_order(db_session, pi_id)

    with patch("app.api.v1.endpoints.stripe_webhook.verify_webhook_signature",
               return_value=make_stripe_event("payment_intent.payment_failed", pi_id)):
        response = client.post(
            "/api/v1/stripe/webhook",
            content=b"fake-payload",
            headers={"stripe-signature": "t=1,v1=fake"},
        )

    assert response.status_code == 200
    db_session.refresh(order)
    assert order.status == OrderStatus.cancelled


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
