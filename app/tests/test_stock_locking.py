"""Stock is read and written under a row lock, at the point an order is created.

Awkward to test honestly: the suite runs on SQLite, which has no SELECT FOR
UPDATE and silently ignores the clause, so a real concurrent test would pass
whether or not the lock is asked for. What can be checked is that the service
asks - and asking is the whole fix, since Postgres is what runs in every
deployed environment.

Implementation-coupled on purpose. The alternative is no test at all, and this
is a race that costs oversold inventory rather than a cosmetic detail.

These used to point at confirm_order_admin, which was where stock moved. It now
moves when the order is created, so that is where the locking has to hold.
"""

import uuid
from decimal import Decimal

import pytest

from app.models.order import OrderStatus
from app.models.product_variant import ProductVariant
from app.schemas.order import CartItemIn, ShippingIn
from app.services.order_service import (
    cancel_order,
    confirm_order_admin,
    create_order,
)
from app.tests.test_orders import make_order, make_product_with_variant


def shipping() -> ShippingIn:
    return ShippingIn(
        name="Jane Smith",
        phone="5551234567",
        address1="123 Main St",
        city="Springfield",
        state="IL",
        zip="62701",
    )


def place(db_session, cart: list[CartItemIn]):
    """An order created the way the application creates one."""
    return create_order(
        db=db_session,
        user_id="test-user-123",
        cart=cart,
        shipping=shipping(),
        stripe_pi_id=f"pi_{uuid.uuid4().hex}",
    )


class RecordingGet:
    """Wraps Session.get, remembering how each variant was fetched."""

    def __init__(self, session):
        self._real = session.get
        self.variant_calls: list[tuple[uuid.UUID, bool]] = []
        self.refreshed: list[bool] = []

    def __call__(self, entity, ident, **kwargs):
        if entity is ProductVariant:
            self.variant_calls.append((ident, kwargs.get("with_for_update", False)))
            self.refreshed.append(kwargs.get("populate_existing", False))
        return self._real(entity, ident, **kwargs)


def test_creating_locks_every_variant_it_takes(db_session, monkeypatch):
    _, variant = make_product_with_variant(db_session, "LOCK-1", stock=5)

    spy = RecordingGet(db_session)
    monkeypatch.setattr(db_session, "get", spy)
    place(db_session, [CartItemIn(variant_id=variant.id, quantity=1)])

    assert spy.variant_calls, "no variant was read at all"
    for variant_id, locked in spy.variant_calls:
        assert locked, f"variant {variant_id} was read without a lock"


def test_creating_rereads_under_the_lock(db_session, monkeypatch):
    """The lock is worthless without populate_existing, and its absence looks fine.

    create_order loads the variants before taking stock, so they are already in
    the identity map. Session.get() then takes the lock but leaves those stale
    attributes alone - the row is correctly locked and the value read from it is
    the one fetched before waiting. That is the lost update the lock exists to
    prevent.

    Verified against real Postgres, where two concurrent buyers both took the
    last unit and both got an order. SQLite ignores FOR UPDATE entirely, so this
    is asserted on the call rather than on the outcome; scripts/check_stock_race.py
    is the version that proves it for real.
    """
    _, variant = make_product_with_variant(db_session, "REREAD-1", stock=5)

    spy = RecordingGet(db_session)
    monkeypatch.setattr(db_session, "get", spy)
    place(db_session, [CartItemIn(variant_id=variant.id, quantity=1)])

    assert spy.refreshed, "no variant was read at all"
    assert all(spy.refreshed), (
        "a variant was locked without being re-read - it would deduct from a "
        "value fetched before the lock was granted"
    )


def test_cancelling_locks_the_variants_it_restores(db_session, monkeypatch):
    order, _ = make_order(db_session, stock=5)

    spy = RecordingGet(db_session)
    monkeypatch.setattr(db_session, "get", spy)
    cancel_order(db_session, order.id)

    assert spy.variant_calls
    for variant_id, locked in spy.variant_calls:
        assert locked, f"variant {variant_id} was restored without a lock"


def test_variants_are_locked_in_a_consistent_order(db_session, monkeypatch):
    """Two orders taking the same rows in opposite orders deadlock, and Postgres
    resolves that by killing one of them. Sorting makes the order the same for
    everyone.

    Two variants is the minimum that can show it, and the cart is deliberately
    built in the opposite order to the sorted one - otherwise the assertion
    holds whether or not anything sorts.
    """
    product, first = make_product_with_variant(db_session, "MULTI-1", stock=10)
    second = ProductVariant(
        product_id=product.id,
        catalog_id="MULTI-1-B",
        size_value=Decimal("50"),
        size_unit="ug",
        price=Decimal("10.00"),
        stock=10,
    )
    db_session.add(second)
    db_session.commit()

    descending = sorted([first.id, second.id], key=str, reverse=True)
    cart = [CartItemIn(variant_id=variant_id, quantity=1) for variant_id in descending]

    spy = RecordingGet(db_session)
    monkeypatch.setattr(db_session, "get", spy)
    place(db_session, cart)

    fetched = [str(variant_id) for variant_id, _ in spy.variant_calls]
    assert len(fetched) == 2, f"expected both variants to be locked, got {fetched}"
    assert fetched == sorted(fetched), fetched


def test_creating_refuses_to_oversell(db_session):
    """The lock must not have changed what the check does."""
    _, variant = make_product_with_variant(db_session, "SHORT-1", stock=0)

    with pytest.raises(ValueError, match="Insufficient stock"):
        place(db_session, [CartItemIn(variant_id=variant.id, quantity=1)])


def test_confirming_no_longer_touches_stock(db_session, monkeypatch):
    """Stock was taken at creation, so confirm has nothing to lock or deduct."""
    order, _ = make_order(db_session, stock=5)

    spy = RecordingGet(db_session)
    monkeypatch.setattr(db_session, "get", spy)
    confirm_order_admin(db_session, order.id)

    assert spy.variant_calls == [], "confirm read a variant it has no business reading"


def test_two_buyers_cannot_both_take_the_last_unit(db_session):
    """The regression this change exists for.

    Both used to get an order and the second only failed when an admin came to
    confirm it - by which time their card was authorised. The second now fails
    at creation, before any order exists.
    """
    _, variant = make_product_with_variant(db_session, "LAST-1", stock=1)
    cart = [CartItemIn(variant_id=variant.id, quantity=1)]

    first = place(db_session, cart)
    assert first is not None

    with pytest.raises(ValueError, match="Insufficient stock"):
        place(db_session, cart)

    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock == 0


def test_a_failed_order_leaves_nothing_behind(db_session):
    """The attempt is rolled back whole - no order, and no half-taken stock."""
    from app.models.order import Order
    from sqlalchemy import func, select

    product, first = make_product_with_variant(db_session, "PARTIAL-1", stock=10)
    short = ProductVariant(
        product_id=product.id,
        catalog_id="PARTIAL-1-B",
        size_value=Decimal("50"),
        size_unit="ug",
        price=Decimal("10.00"),
        stock=0,
    )
    db_session.add(short)
    db_session.commit()

    before = db_session.scalar(select(func.count()).select_from(Order))

    with pytest.raises(ValueError, match="Insufficient stock"):
        place(
            db_session,
            [
                CartItemIn(variant_id=first.id, quantity=1),
                CartItemIn(variant_id=short.id, quantity=1),
            ],
        )

    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Order)) == before
    assert db_session.get(ProductVariant, first.id).stock == 10, (
        "the variant that had stock was decremented for an order that never existed"
    )


def test_two_lines_for_one_variant_are_counted_together(db_session):
    """A cart may hold the same variant twice. Checked one line at a time, 3 + 3
    against a stock of 5 passes twice and oversells."""
    _, variant = make_product_with_variant(db_session, "DUPE-1", stock=5)

    with pytest.raises(ValueError, match="Insufficient stock"):
        place(
            db_session,
            [
                CartItemIn(variant_id=variant.id, quantity=3),
                CartItemIn(variant_id=variant.id, quantity=3),
            ],
        )

    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock == 5


def test_cancelling_an_unconfirmed_order_returns_its_stock(db_session):
    """It holds stock from creation, so cancelling before confirm must give it
    back. Under the old rule nothing was deducted until confirm and this
    restored nothing."""
    _, variant = make_product_with_variant(db_session, "EARLY-1", stock=4)
    order = place(db_session, [CartItemIn(variant_id=variant.id, quantity=3)])
    order.status = OrderStatus.awaiting_fulfillment
    db_session.commit()

    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock == 1

    cancel_order(db_session, order.id)

    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock == 4


def test_taking_stock_writes_nothing_when_any_variant_is_short(db_session):
    """_take_stock validates in full before writing anything.

    Tested directly rather than through create_order, which rolls back on
    failure and would hide a partial write. The guarantee belongs to the helper:
    a future caller that does not roll back must still be safe.

    The ids are fixed rather than random because _lock_in_order sorts by id, and
    the write only happens before the failure if the variant with stock is
    reached first. With uuid4 this test caught a partial write about half the
    time, which is worse than not having it.
    """
    from app.services.order_service import _aggregate_demand, _take_stock

    product, _ = make_product_with_variant(db_session, "ATOMIC-1", stock=1)

    plenty = ProductVariant(
        id=uuid.UUID("00000000-0000-0000-0000-00000000000a"),
        product_id=product.id,
        catalog_id="ATOMIC-1-PLENTY",
        size_value=Decimal("100"),
        size_unit="g",
        price=Decimal("10.00"),
        stock=10,
    )
    short = ProductVariant(
        id=uuid.UUID("00000000-0000-0000-0000-00000000000b"),
        product_id=product.id,
        catalog_id="ATOMIC-1-SHORT",
        size_value=Decimal("50"),
        size_unit="ug",
        price=Decimal("10.00"),
        stock=0,
    )
    db_session.add_all([plenty, short])
    db_session.commit()

    assert str(plenty.id) < str(short.id), "the plentiful variant must be locked first"

    demand = _aggregate_demand([(plenty.id, 1, "plenty"), (short.id, 1, "short")])

    with pytest.raises(ValueError, match="Insufficient stock"):
        _take_stock(db_session, demand)

    assert plenty.stock == 10, "a variant was decremented for a take that failed"
    assert short.stock == 0


def test_a_customer_cancelling_their_own_order_returns_the_stock(
    user_client, db_session
):
    """The customer-facing cancel must give stock back, like the admin one does.

    This was correct until stock moved to creation: the endpoint only permits
    pending and awaiting_fulfillment, and under the old rule neither held any
    stock, so setting the status was the whole job. Now every order holds stock
    from the moment it exists, and cancelling without returning it destroys
    inventory permanently.
    """
    from unittest.mock import patch

    _, variant = make_product_with_variant(db_session, "CUSTCANCEL-1", stock=5)
    order = place(db_session, [CartItemIn(variant_id=variant.id, quantity=3)])
    order.user_id = "test-user-123"
    order.status = OrderStatus.awaiting_fulfillment
    db_session.commit()

    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock == 2, "creation should have taken 3"

    with patch("app.api.v1.endpoints.orders.cancel_payment_intent"):
        response = user_client.post(f"/api/v1/orders/{order.id}/cancel")

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock == 5, (
        "the customer cancelled and the stock was destroyed"
    )


def test_the_service_level_customer_cancel_returns_stock_too(db_session):
    """cancel_order_by_customer reads like the customer path and is exported.

    Only the tests reach it today, but the moment anything routes to it the same
    inventory leak appears - so it must not be able to diverge from the endpoint.
    """
    from app.services.order_service import cancel_order_by_customer

    _, variant = make_product_with_variant(db_session, "CUSTCANCEL-2", stock=4)
    order = place(db_session, [CartItemIn(variant_id=variant.id, quantity=2)])
    order.user_id = "test-user-123"
    order.status = OrderStatus.awaiting_fulfillment
    db_session.commit()

    cancel_order_by_customer(db_session, order.id, "test-user-123")

    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock == 4
