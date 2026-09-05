"""Stock is read and written under a row lock.

Awkward to test honestly: the suite runs on SQLite, which has no SELECT FOR
UPDATE and silently ignores the clause, so a real concurrent test would pass
whether or not the lock is asked for. What can be checked is that the service
asks - and asking is the whole fix, since Postgres is what runs in every
deployed environment.

Implementation-coupled on purpose. The alternative is no test at all, and this
is a race that costs oversold inventory rather than a cosmetic detail.
"""

import uuid
from decimal import Decimal

from app.models.order import OrderStatus
from app.models.product_variant import ProductVariant
from app.services.order_service import cancel_order, confirm_order_admin
from app.tests.test_orders import make_order, make_product_with_variant


class RecordingGet:
    """Wraps Session.get, remembering how each variant was fetched."""

    def __init__(self, session):
        self._real = session.get
        self.variant_calls: list[tuple[uuid.UUID, bool]] = []

    def __call__(self, entity, ident, **kwargs):
        if entity is ProductVariant:
            self.variant_calls.append((ident, kwargs.get("with_for_update", False)))
        return self._real(entity, ident, **kwargs)


def test_confirming_locks_every_variant_it_deducts(db_session, monkeypatch):
    order, variant = make_order(db_session, stock=5)

    spy = RecordingGet(db_session)
    monkeypatch.setattr(db_session, "get", spy)
    confirm_order_admin(db_session, order.id)

    assert spy.variant_calls, "no variant was read at all"
    for variant_id, locked in spy.variant_calls:
        assert locked, f"variant {variant_id} was read without a lock"


def test_cancelling_locks_the_variants_it_restores(db_session, monkeypatch):
    order, variant = make_order(db_session, stock=5)
    confirm_order_admin(db_session, order.id)

    spy = RecordingGet(db_session)
    monkeypatch.setattr(db_session, "get", spy)
    cancel_order(db_session, order.id)

    assert spy.variant_calls
    for variant_id, locked in spy.variant_calls:
        assert locked, f"variant {variant_id} was restored without a lock"


def test_variants_are_locked_in_a_consistent_order(db_session, monkeypatch):
    """Two confirms taking the same rows in opposite orders deadlock, and
    Postgres resolves that by killing one of them. Sorting makes the order the
    same for everyone."""
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

    order, _ = make_order(db_session, stock=10)
    order.items[0].variant_id = second.id
    from app.models.order import OrderItem

    order.items.append(
        OrderItem(
            variant_id=first.id,
            product_name=product.name,
            variant_label="100g",
            unit_price=Decimal("10.00"),
            quantity=1,
        )
    )
    db_session.commit()

    spy = RecordingGet(db_session)
    monkeypatch.setattr(db_session, "get", spy)
    confirm_order_admin(db_session, order.id)

    fetched = [str(variant_id) for variant_id, _ in spy.variant_calls]
    assert fetched == sorted(fetched), fetched


def test_confirming_still_refuses_to_oversell(db_session):
    """The lock must not have changed what the check does."""
    order, variant = make_order(db_session, stock=0)

    try:
        confirm_order_admin(db_session, order.id)
    except ValueError as error:
        assert "Insufficient stock" in str(error)
    else:
        raise AssertionError("confirmed an order it had no stock for")

    db_session.expire_all()
    assert db_session.get(type(order), order.id).status == OrderStatus.awaiting_fulfillment
