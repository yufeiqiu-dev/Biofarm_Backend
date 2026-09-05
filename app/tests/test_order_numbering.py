"""order_number is a human-facing sequential integer over a unique column.

It is derived as max + 1, which is a read-then-write race. Two checkouts landing
together read the same maximum, and the second insert violates the constraint.
Unhandled that surfaced as a 500 on a request whose card was already authorized:
the customer is charged and has no order, and the only record of it is a
traceback.
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.order import Order, OrderStatus
from app.schemas.order import CartItemIn, ShippingIn
from app.services.order_service import create_order
from app.tests.test_orders import make_product_with_variant

SHIPPING = ShippingIn(
    name="Jane Smith",
    phone="5551234567",
    address1="123 Main St",
    city="Springfield",
    state="IL",
    zip="62701",
)


def _create(db_session, variant, pi_suffix: str) -> Order:
    return create_order(
        db=db_session,
        user_id="num-user",
        cart=[CartItemIn(variant_id=variant.id, quantity=1)],
        shipping=SHIPPING,
        stripe_pi_id=f"pi_{pi_suffix}_{uuid.uuid4().hex[:8]}",
    )


def test_numbering_starts_at_1000(db_session):
    _, variant = make_product_with_variant(db_session, "NUM-01")
    assert _create(db_session, variant, "a").order_number == 1000


def test_numbering_increments(db_session):
    _, variant = make_product_with_variant(db_session, "NUM-02")
    numbers = [_create(db_session, variant, str(i)).order_number for i in range(3)]
    assert numbers == [1000, 1001, 1002]


def test_a_taken_number_is_retried_rather_than_raising(db_session):
    """Simulates losing the race: the number create_order picks is already gone
    by the time it commits. It must take the next one, not 500."""
    _, variant = make_product_with_variant(db_session, "NUM-03")
    _create(db_session, variant, "first")  # takes 1000

    real_next = None

    def steal_the_number(db):
        """Return 1000 once - a number that is already taken - then behave."""
        nonlocal real_next
        if real_next is None:
            real_next = True
            return 1000
        return 1001

    with patch("app.services.order_service._next_order_number", side_effect=steal_the_number):
        order = _create(db_session, variant, "second")

    assert order.order_number == 1001
    assert order.status == OrderStatus.pending


def test_retries_are_bounded(db_session):
    """A number that never becomes available must eventually surface, not spin."""
    _, variant = make_product_with_variant(db_session, "NUM-04")
    _create(db_session, variant, "first")  # takes 1000

    with patch("app.services.order_service._next_order_number", return_value=1000):
        with pytest.raises(IntegrityError):
            _create(db_session, variant, "second")


def test_a_duplicate_payment_intent_is_not_retried(db_session):
    """orders has a second unique column. Retrying a conflict on that one would
    fail five times and bury the real cause, so it must surface immediately."""
    _, variant = make_product_with_variant(db_session, "NUM-05")
    pi_id = f"pi_shared_{uuid.uuid4().hex[:8]}"

    create_order(
        db=db_session,
        user_id="num-user",
        cart=[CartItemIn(variant_id=variant.id, quantity=1)],
        shipping=SHIPPING,
        stripe_pi_id=pi_id,
    )

    with patch("app.services.order_service._next_order_number", wraps=lambda db: 5000) as spy:
        with pytest.raises(IntegrityError):
            create_order(
                db=db_session,
                user_id="num-user",
                cart=[CartItemIn(variant_id=variant.id, quantity=1)],
                shipping=SHIPPING,
                stripe_pi_id=pi_id,
            )

    assert spy.call_count == 1, "a payment-intent conflict was retried"


def test_the_order_is_intact_after_a_retry(db_session):
    """A rolled-back attempt leaves its OrderItem instances unusable, so the
    retry has to rebuild them. This checks the rebuilt order is complete."""
    product, variant = make_product_with_variant(db_session, "NUM-06", price=12.50)
    _create(db_session, variant, "first")

    numbers = iter([1000, 1001])
    with patch("app.services.order_service._next_order_number", side_effect=lambda db: next(numbers)):
        order = create_order(
            db=db_session,
            user_id="num-user",
            cart=[CartItemIn(variant_id=variant.id, quantity=3)],
            shipping=SHIPPING,
            stripe_pi_id=f"pi_{uuid.uuid4().hex}",
            customer_email="jane@example.com",
        )

    assert order.order_number == 1001
    assert order.total_amount == Decimal("37.50")
    assert order.customer_email == "jane@example.com"
    assert len(order.items) == 1
    assert order.items[0].quantity == 3
    assert order.items[0].unit_price == Decimal("12.50")
    assert order.items[0].product_name == product.name
