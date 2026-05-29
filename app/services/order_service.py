import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.checkout_session import CheckoutSession
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product_variant import ProductVariant
from app.schemas.order import CartItemIn, ShippingIn


def _next_order_number(db: Session) -> int:
    max_num = db.scalar(select(func.max(Order.order_number)))
    return (max_num or 999) + 1


def _load_order(db: Session, order_id: uuid.UUID) -> Order | None:
    return db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
    )


def _load_order_by_pi(db: Session, pi_id: str) -> Order | None:
    return db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.stripe_payment_intent_id == pi_id)
    )


def create_order(
    db: Session,
    user_id: str,
    cart: list[CartItemIn],
    shipping: ShippingIn,
    stripe_pi_id: str,
    tax_amount: Decimal = Decimal("0"),
    customer_email: str = "",
    card_brand: str = "",
    card_last4: str = "",
) -> Order:
    variant_ids = [item.variant_id for item in cart]
    variants = {
        v.id: v
        for v in db.scalars(
            select(ProductVariant)
            .options(selectinload(ProductVariant.product))
            .where(ProductVariant.id.in_(variant_ids))
        ).all()
    }

    items = []
    total = Decimal("0")
    for cart_item in cart:
        variant = variants.get(cart_item.variant_id)
        if variant is None:
            raise ValueError(f"Variant {cart_item.variant_id} not found")
        line = variant.price * cart_item.quantity
        total += line
        items.append(
            OrderItem(
                variant_id=variant.id,
                product_name=variant.product.name if variant.product else "Unknown",
                variant_label=f"{variant.size_value} {variant.size_unit}",
                unit_price=variant.price,
                quantity=cart_item.quantity,
            )
        )

    order = Order(
        order_number=_next_order_number(db),
        user_id=user_id,
        customer_email=customer_email,
        card_brand=card_brand,
        card_last4=card_last4,
        status=OrderStatus.pending,
        stripe_payment_intent_id=stripe_pi_id,
        shipping_name=shipping.name,
        shipping_phone=shipping.phone,
        shipping_address1=shipping.address1,
        shipping_address2=shipping.address2,
        shipping_city=shipping.city,
        shipping_state=shipping.state,
        shipping_zip=shipping.zip,
        notes=shipping.notes,
        total_amount=total,
        tax_amount=tax_amount,
        items=items,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def save_checkout_session(
    db: Session,
    stripe_pi_id: str,
    user_id: str,
    cart: list[CartItemIn],
    shipping: ShippingIn,
    tax_amount_cents: int = 0,
    customer_email: str = "",
) -> CheckoutSession:
    session = CheckoutSession(
        stripe_pi_id=stripe_pi_id,
        user_id=user_id,
        customer_email=customer_email,
        cart_json=json.dumps([{"variant_id": str(item.variant_id), "quantity": item.quantity} for item in cart]),
        shipping_json=shipping.model_dump_json(),
        tax_amount_cents=tax_amount_cents,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def create_order_from_checkout_session(
    db: Session,
    stripe_pi_id: str,
    card_brand: str = "",
    card_last4: str = "",
) -> Order | None:
    session = db.scalar(
        select(CheckoutSession).where(CheckoutSession.stripe_pi_id == stripe_pi_id)
    )
    if session is None:
        return None

    cart = [CartItemIn(variant_id=item["variant_id"], quantity=item["quantity"]) for item in json.loads(session.cart_json)]
    shipping = ShippingIn.model_validate_json(session.shipping_json)
    tax_amount = Decimal(session.tax_amount_cents) / 100

    order = create_order(db, user_id=session.user_id, cart=cart, shipping=shipping, stripe_pi_id=stripe_pi_id, tax_amount=tax_amount, customer_email=session.customer_email, card_brand=card_brand, card_last4=card_last4)
    order.status = OrderStatus.awaiting_fulfillment
    db.delete(session)
    db.commit()
    db.refresh(order)
    return order


def delete_checkout_session(db: Session, stripe_pi_id: str) -> None:
    session = db.scalar(
        select(CheckoutSession).where(CheckoutSession.stripe_pi_id == stripe_pi_id)
    )
    if session:
        db.delete(session)
        db.commit()


def cleanup_stale_checkout_sessions(db: Session, max_age_days: int = 8) -> int:
    """Delete sessions older than max_age_days. Called at startup to catch any
    that never received a payment_intent.canceled webhook (e.g. backend was down)."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    stale = db.scalars(
        select(CheckoutSession).where(CheckoutSession.created_at < cutoff)
    ).all()
    count = len(stale)
    for session in stale:
        db.delete(session)
    if count:
        db.commit()
    return count


def get_orders_for_user(db: Session, user_id: str) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
        ).all()
    )


def get_order_by_id(db: Session, order_id: uuid.UUID) -> Order | None:
    return _load_order(db, order_id)


def get_order_by_payment_intent(db: Session, pi_id: str) -> Order | None:
    return _load_order_by_pi(db, pi_id)


def confirm_order_admin(db: Session, order_id: uuid.UUID) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status != OrderStatus.awaiting_fulfillment:
        raise ValueError(f"Cannot confirm order in status {order.status.value}")

    # Validate stock before committing
    for item in order.items:
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant and variant.stock < item.quantity:
                raise ValueError(
                    f"Insufficient stock for {item.product_name}: "
                    f"need {item.quantity}, only {variant.stock} available"
                )

    # Deduct stock — admin is setting aside inventory to fulfil this order
    for item in order.items:
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant:
                variant.stock -= item.quantity

    order.status = OrderStatus.confirmed
    db.commit()
    db.refresh(order)
    return order


def ship_order(db: Session, order_id: uuid.UUID, tracking_number: str | None = None) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status != OrderStatus.confirmed:
        raise ValueError(f"Cannot ship order in status {order.status.value}")
    order.status = OrderStatus.shipped
    if tracking_number:
        order.tracking_number = tracking_number
    db.commit()
    db.refresh(order)
    return order


def update_tracking_number(db: Session, order_id: uuid.UUID, tracking_number: str) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status not in (OrderStatus.shipped, OrderStatus.delivered):
        raise ValueError("Tracking number can only be set on shipped or delivered orders")
    order.tracking_number = tracking_number
    db.commit()
    db.refresh(order)
    return order


def deliver_order(db: Session, order_id: uuid.UUID) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status != OrderStatus.shipped:
        raise ValueError(f"Cannot deliver order in status {order.status.value}")
    order.status = OrderStatus.delivered
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: uuid.UUID) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status == OrderStatus.cancelled:
        raise ValueError(f"Order is already cancelled")

    # Stock was deducted at confirm — restore it if cancelling after that point
    if order.status in (OrderStatus.confirmed, OrderStatus.shipped, OrderStatus.delivered):
        for item in order.items:
            if item.variant_id:
                variant = db.get(ProductVariant, item.variant_id)
                if variant:
                    variant.stock += item.quantity

    order.status = OrderStatus.cancelled
    db.commit()
    db.refresh(order)
    return order


def cancel_order_by_customer(db: Session, order_id: uuid.UUID, user_id: str) -> Order:
    order = _load_order(db, order_id)
    if order is None or order.user_id != user_id:
        raise ValueError("Order not found")
    if order.status != OrderStatus.awaiting_fulfillment:
        raise ValueError(f"Cannot cancel order in status {order.status.value}")
    order.status = OrderStatus.cancelled
    db.commit()
    db.refresh(order)
    return order


def list_all_orders(db: Session, status: OrderStatus | None = None) -> list[Order]:
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .order_by(Order.created_at.desc())
    )
    if status:
        stmt = stmt.where(Order.status == status)
    return list(db.scalars(stmt).all())
