import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.checkout_session import CheckoutSession
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product_variant import ProductVariant
from app.schemas.order import CartItemIn, ShippingIn
from app.services.order_numbers import generate_order_number


# How many times to re-roll an order number when the generated one is already
# taken. Against 32^8 possibilities a single collision is already unlikely; five
# in a row is not worth planning for beyond failing loudly.
_ORDER_NUMBER_ATTEMPTS = 5


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

    # Plain values rather than ORM objects: a retry below needs to build fresh
    # OrderItems, and an instance from a rolled-back attempt cannot be reused.
    item_values: list[dict] = []
    total = Decimal("0")
    for cart_item in cart:
        variant = variants.get(cart_item.variant_id)
        if variant is None:
            raise ValueError(f"Variant {cart_item.variant_id} not found")
        total += variant.price * cart_item.quantity
        item_values.append(
            {
                "variant_id": variant.id,
                "product_name": variant.product.name if variant.product else "Unknown",
                "variant_label": f"{variant.size_value} {variant.size_unit}",
                "unit_price": variant.price,
                "quantity": cart_item.quantity,
            }
        )

    def build(order_number: str) -> Order:
        return Order(
            order_number=order_number,
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
            items=[OrderItem(**values) for values in item_values],
        )

    # The number is random rather than derived from what other rows hold, so the
    # old read-then-write race is gone entirely. The retry stays for the far
    # rarer case of two generated values colliding, and because an unhandled
    # IntegrityError here is a 500 on a request whose card is already authorized
    # - the customer charged, with no order.
    for attempt in range(_ORDER_NUMBER_ATTEMPTS):
        order = build(generate_order_number())
        db.add(order)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # orders has a second unique column, stripe_payment_intent_id, and
            # retrying a conflict on that one would just fail five times and
            # hide the real cause. If an order for this intent now exists, the
            # collision was not the number.
            if _load_order_by_pi(db, stripe_pi_id) is not None:
                raise
            if attempt == _ORDER_NUMBER_ATTEMPTS - 1:
                raise
            continue
        db.refresh(order)
        return order

    raise RuntimeError("unreachable")  # pragma: no cover


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
    """Delete sessions older than max_age_days.

    A session that never received either a payment or a payment_intent.canceled
    webhook - the backend was down, or the customer simply closed the tab - is
    otherwise there forever. Run from app.jobs.cleanup on a schedule; this used
    to run in the startup hook, which meant a table scan and delete at the exact
    moment the service was trying to become healthy.
    """
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

    # Aggregate demand per variant (an order may have multiple items for the same variant)
    demand: dict[uuid.UUID, tuple[int, str]] = {}  # variant_id → (total_qty, product_name)
    for item in order.items:
        if item.variant_id:
            qty, name = demand.get(item.variant_id, (0, item.product_name))
            demand[item.variant_id] = (qty + item.quantity, name)

    # Validate then deduct in one pass — avoids per-item checks that miss combined overstock
    for variant_id, (qty, name) in demand.items():
        variant = db.get(ProductVariant, variant_id)
        if variant:
            if variant.stock < qty:
                raise ValueError(
                    f"Insufficient stock for {name}: need {qty}, only {variant.stock} available"
                )
            variant.stock -= qty

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
