"""Customer-facing order numbers.

These were a sequential integer from 1000. That is guessable, and it publishes
information the business would not choose to: two orders a month apart give the
exact number of orders taken in between. It was also a read-then-write race,
since "max + 1" depends on what every other row holds.

They are now ten random digits - see app/services/order_numbers.py. These tests
cover the two things that matter: that the value is unguessable, and that its
shape survives a spreadsheet and a phone call.
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.order import Order, OrderStatus
from app.schemas.order import CartItemIn, ShippingIn
from app.services.order_numbers import (
    LENGTH,
    generate_order_number,
    looks_like_an_order_number,
)
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


def _create(db_session, variant, quantity: int = 1) -> Order:
    return create_order(
        db=db_session,
        user_id="num-user",
        cart=[CartItemIn(variant_id=variant.id, quantity=quantity)],
        shipping=SHIPPING,
        stripe_pi_id=f"pi_{uuid.uuid4().hex}",
    )


# --- the generator ---

def test_the_shape_is_stable():
    number = generate_order_number()
    assert len(number) == LENGTH
    assert looks_like_an_order_number(number)


def test_it_is_only_digits():
    """The whole point of this format. Everything downstream of an order number
    tends to want digits: accounting software, spreadsheets, a numeric keypad."""
    for _ in range(500):
        assert generate_order_number().isdigit()


def test_it_never_starts_with_zero():
    """A leading zero survives the database and the API perfectly well and then
    vanishes the moment someone pastes the number into a spreadsheet - which is
    where order numbers spend a lot of their life."""
    for _ in range(500):
        assert not generate_order_number().startswith("0")


def test_a_small_sample_does_not_repeat():
    """Deliberately 200 and not more.

    With 9e9 possibilities the birthday bound makes a large sample genuinely
    likely to collide - 5,000 draws collide about 0.14% of the time, which is a
    test that fails once every few hundred CI runs for no reason. At 200 the
    chance is around one in half a million, so a failure here means the
    generator stopped being random rather than that the dice were unlucky.

    Collision *handling* is covered by test_a_collision_is_retried_rather_than_raised.
    """
    numbers = {generate_order_number() for _ in range(200)}
    assert len(numbers) == 200


def test_it_uses_a_cryptographic_source():
    """Predicting the next number from the last should not be possible. The API
    checks ownership separately, so this is defence in depth rather than the
    only control - but a guessable identifier invites the attempt."""
    import app.services.order_numbers as module

    assert module.secrets.__name__ == "secrets"
    source = (module.__file__ or "")
    assert source
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "import random" not in text


@pytest.mark.parametrize(
    "value",
    [
        "",
        "1000",  # the old sequential format
        "482719305",  # one short
        "48271930567",  # one long
        "0827193056",  # leading zero
        "48271930a6",
        "OB-7K3M9QXZ",  # the previous format
        "٤٨٢٧١٩٣٠٥٦",  # Arabic-Indic digits: isdigit() alone accepts these
    ],
)
def test_malformed_values_are_recognised_as_such(value):
    assert not looks_like_an_order_number(value)


# --- through the service ---

def test_an_order_gets_a_well_formed_number(db_session):
    _, variant = make_product_with_variant(db_session, "NUM-01")
    order = _create(db_session, variant)
    assert looks_like_an_order_number(order.order_number)


def test_consecutive_orders_are_not_consecutive_numbers(db_session):
    """The whole point. Sequential numbers let anyone who places two orders read
    off how many the business took in between."""
    _, variant = make_product_with_variant(db_session, "NUM-02", stock=10)

    numbers = [_create(db_session, variant).order_number for _ in range(3)]

    assert len(set(numbers)) == 3
    # The old scheme produced 1000, 1001, 1002. Consecutive values would mean it
    # is back, whatever the format looks like.
    as_ints = sorted(int(n) for n in numbers)
    assert as_ints[1] - as_ints[0] != 1 or as_ints[2] - as_ints[1] != 1, numbers


def test_a_collision_is_retried_rather_than_raised(db_session):
    """Vanishingly unlikely in practice, but an unhandled IntegrityError here is
    a 500 on a request whose card is already authorized - charged, with no
    order."""
    _, variant = make_product_with_variant(db_session, "NUM-03", stock=10)
    taken = _create(db_session, variant).order_number

    # First call returns a number already in use, then a fresh one.
    numbers = iter([taken, generate_order_number()])
    with patch(
        "app.services.order_service.generate_order_number", side_effect=lambda: next(numbers)
    ):
        order = _create(db_session, variant)

    assert order.order_number != taken
    assert order.status == OrderStatus.pending


def test_retries_are_bounded(db_session):
    """A generator stuck on one value must surface, not spin."""
    _, variant = make_product_with_variant(db_session, "NUM-04", stock=10)
    taken = _create(db_session, variant).order_number

    with patch("app.services.order_service.generate_order_number", return_value=taken):
        with pytest.raises(IntegrityError):
            _create(db_session, variant)


def test_a_duplicate_payment_intent_is_not_retried(db_session):
    """orders has a second unique column. Retrying a conflict on that one would
    fail five times and bury the real cause."""
    _, variant = make_product_with_variant(db_session, "NUM-05", stock=10)
    pi_id = f"pi_shared_{uuid.uuid4().hex[:8]}"

    create_order(
        db=db_session,
        user_id="num-user",
        cart=[CartItemIn(variant_id=variant.id, quantity=1)],
        shipping=SHIPPING,
        stripe_pi_id=pi_id,
    )

    with patch(
        "app.services.order_service.generate_order_number", wraps=generate_order_number
    ) as spy:
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
    retry rebuilds them. This checks the rebuilt order is complete."""
    product, variant = make_product_with_variant(db_session, "NUM-06", price=12.50, stock=10)
    taken = _create(db_session, variant).order_number

    numbers = iter([taken, generate_order_number()])
    with patch(
        "app.services.order_service.generate_order_number", side_effect=lambda: next(numbers)
    ):
        order = create_order(
            db=db_session,
            user_id="num-user",
            cart=[CartItemIn(variant_id=variant.id, quantity=3)],
            shipping=SHIPPING,
            stripe_pi_id=f"pi_{uuid.uuid4().hex}",
            customer_email="jane@example.com",
        )

    assert order.order_number != taken
    assert order.total_amount == Decimal("37.50")
    assert order.customer_email == "jane@example.com"
    assert len(order.items) == 1
    assert order.items[0].quantity == 3
    assert order.items[0].unit_price == Decimal("12.50")
    assert order.items[0].product_name == product.name
