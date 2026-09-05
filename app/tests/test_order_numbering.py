"""Customer-facing order numbers.

These were a sequential integer from 1000. That is guessable, and it publishes
information the business would not choose to: two orders a month apart give the
exact number of orders taken in between. It was also a read-then-write race,
since "max + 1" depends on what every other row holds.

They are now a random string - see app/services/order_numbers.py. These tests
cover the two things that matter about that: that the value is unguessable and
unique, and that the shape survives being read down a phone line.
"""

import re
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.order import Order, OrderStatus
from app.schemas.order import CartItemIn, ShippingIn
from app.services.order_numbers import (
    ALPHABET,
    LENGTH,
    PREFIX,
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
    assert number.startswith(PREFIX)
    assert len(number) == len(PREFIX) + LENGTH
    assert looks_like_an_order_number(number)


def test_the_alphabet_avoids_characters_people_confuse():
    """These get read down a phone line and typed into support emails. I, L, O
    and U are the ones mistaken for 1, 1, 0 and V, and a customer reading a
    number aloud has no way to disambiguate them."""
    for character in "ILOU":
        assert character not in ALPHABET, character

    for _ in range(200):
        body = generate_order_number()[len(PREFIX):]
        assert all(c in ALPHABET for c in body), body


def test_it_is_uppercase_so_case_never_has_to_be_communicated():
    for _ in range(50):
        number = generate_order_number()
        assert number == number.upper()


def test_numbers_do_not_repeat():
    """32^8 is a little over a trillion, so a collision in this sample would mean
    the generator is not actually random."""
    numbers = {generate_order_number() for _ in range(5000)}
    assert len(numbers) == 5000


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
        "1000",
        "OB-",
        "OB-SHORT",
        "OB-TOOLONGVALUE",
        "XX-7K3M9QXZ",
        "OB-7K3M9QXI",  # I is not in the alphabet
        "ob-7k3m9qxz",  # lowercase
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
    bodies = [n[len(PREFIX):] for n in numbers]
    assert not all(re.fullmatch(r"\d+", b) for b in bodies), bodies


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
