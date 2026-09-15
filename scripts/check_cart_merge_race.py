"""Prove that concurrent cart writes for one customer do not lose each other.

**Not part of the test suite, and it cannot be.** `app/tests/` runs on
in-memory SQLite, which silently ignores `SELECT ... FOR UPDATE` - so a
concurrency test there passes whether or not the locking is correct. The suite
asserts that the service *asks* for the lock; this is the version that knows.

Two races, both against one customer's basket:

1. **clear_bought_lines** - two orders for one customer sharing a variant, both
   webhooks landing at once. Without the lock the read-modify-write on
   `quantity` loses a decrement: both read 5, one writes 3, the other writes 4
   from its stale read. Every decrement must land. This one is deterministic -
   it fails every run without `.with_for_update()`.

2. **merge_cart** - two devices whose idle-debounce syncs overlap on the same
   variant with different client clocks. Without the lock, an older writer that
   commits after a newer one overwrites it (both read the stale `existing`,
   both judge themselves newer, both UPDATE unconditionally). This scenario is
   timing-sensitive - a probe outside the transaction cannot force the losing
   interleave reliably - so it is a smoke check, not the guard. The guard for
   the shared mechanism is scenario 1 plus the FOR UPDATE assertions in
   test_cart_sync.py.

Usage - needs the local Postgres up and `.env` pointing at it:

    PYTHONPATH=. .venv/Scripts/python.exe scripts/check_cart_merge_race.py [writers]

It creates cart rows for a throwaway owner, races them, checks the outcome, and
deletes the rows it made. Safe against a development database.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.cart_item import CartItem
from app.models.product_variant import ProductVariant
from app.services.cart_service import (
    CartLineMerge,
    CartOwner,
    clear_bought_lines,
    clear_cart,
    merge_cart,
    set_line,
)

MERGE_OWNER = CartOwner.user("cart-merge-race")
CLEAR_OWNER = CartOwner.user("cart-clear-race")
DEADLOCK_OWNER = CartOwner.user("cart-deadlock-race")


def _pick_variant() -> tuple:
    db = SessionLocal()
    try:
        variant = db.scalars(select(ProductVariant).limit(1)).first()
        if variant is None:
            return None, None
        return variant.id, variant.catalog_id
    finally:
        db.close()


def _cleanup(owner: CartOwner) -> None:
    db = SessionLocal()
    try:
        db.execute(
            delete(CartItem).where(
                CartItem.owner_kind == owner.kind, CartItem.owner_id == owner.id
            )
        )
        db.commit()
    finally:
        db.close()


def race_merge(variant_id, writers: int) -> bool:
    """One device syncs a genuinely newer edit; several others race it with an
    older one. Without the row lock, an older writer that commits after the
    newer one overwrites it (all writers judge themselves newer than the stale
    `existing` they read and UPDATE unconditionally). With the lock, the older
    writers block, re-read the newer value, and skip.
    """
    _cleanup(MERGE_OWNER)
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    newer = base + timedelta(minutes=30)
    older = base + timedelta(minutes=1)
    WINNER_QTY = 777

    seed = SessionLocal()
    try:
        set_line(seed, MERGE_OWNER, variant_id, 1)
        seed.execute(
            CartItem.__table__.update()
            .where(CartItem.owner_id == MERGE_OWNER.id)
            .values(client_updated_at=base)
        )
        seed.commit()
    finally:
        seed.close()

    # One writer with the newest clock, the rest with an older one.
    edits = [(WINNER_QTY, newer)] + [(1, older)] * (writers - 1)
    start = threading.Barrier(writers)
    errors: list[str] = []

    def push(quantity: int, clock: datetime) -> None:
        db = SessionLocal()
        try:
            start.wait()
            merge_cart(
                db,
                MERGE_OWNER,
                [CartLineMerge(variant_id=variant_id, quantity=quantity, client_updated_at=clock)],
            )
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            errors.append(f"{type(error).__name__}: {str(error)[:80]}")
        finally:
            db.close()

    threads = [threading.Thread(target=push, args=e) for e in edits]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = SessionLocal()
    try:
        row = check.scalars(
            select(CartItem).where(CartItem.owner_id == MERGE_OWNER.id)
        ).first()
        final_qty = row.quantity if row else None
    finally:
        check.close()

    ok = not errors and final_qty == WINNER_QTY
    print(f"  merge_cart: final quantity {final_qty} (want {WINNER_QTY}, the newest client clock's edit)")
    for e in errors:
        print(f"    error: {e}")
    print(f"  {'PASS' if ok else 'FAIL - an older edit overwrote a newer one'}")
    _cleanup(MERGE_OWNER)
    return ok


def race_clear(variant_id, writers: int) -> bool:
    _cleanup(CLEAR_OWNER)

    seed = SessionLocal()
    try:
        set_line(seed, CLEAR_OWNER, variant_id, writers)
        seed.commit()
    finally:
        seed.close()

    start = threading.Barrier(writers)
    errors: list[str] = []

    def buy_one() -> None:
        db = SessionLocal()
        try:
            start.wait()
            clear_bought_lines(db, CLEAR_OWNER, [(variant_id, 1)])
            db.commit()
        except Exception as error:  # noqa: BLE001
            errors.append(f"{type(error).__name__}: {str(error)[:80]}")
        finally:
            db.close()

    threads = [threading.Thread(target=buy_one) for _ in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = SessionLocal()
    try:
        row = check.scalars(
            select(CartItem).where(CartItem.owner_id == CLEAR_OWNER.id)
        ).first()
        # writers decrements of 1 from a start of `writers` -> 0 -> tombstoned.
        final_qty = row.quantity if row else None
        tombstoned = bool(row and row.deleted_at is not None)
    finally:
        check.close()

    ok = not errors and tombstoned
    print(f"  clear_bought_lines: final quantity {final_qty}, tombstoned={tombstoned} (want tombstoned, every decrement landed)")
    for e in errors:
        print(f"    error: {e}")
    print(f"  {'PASS' if ok else 'FAIL - a decrement was lost to a stale read'}")
    _cleanup(CLEAR_OWNER)
    return ok


def race_clear_vs_bought(variant_ids) -> bool:
    """`clear_cart` (customer empties on one device) against `clear_bought_lines`
    (a purchase webhook) for the same owner, over several shared rows. If the
    two paths lock rows in different orders, Postgres aborts one with a
    deadlock. Both must lock in variant_id order.
    """
    _cleanup(DEADLOCK_OWNER)
    seed = SessionLocal()
    try:
        for vid in variant_ids:
            set_line(seed, DEADLOCK_OWNER, vid, 3)
        seed.commit()
    finally:
        seed.close()

    start = threading.Barrier(2)
    errors: list[str] = []

    def do_clear_cart() -> None:
        db = SessionLocal()
        try:
            start.wait()
            clear_cart(db, DEADLOCK_OWNER)
        except Exception as error:  # noqa: BLE001
            errors.append(f"clear_cart: {type(error).__name__}: {str(error)[:80]}")
        finally:
            db.close()

    def do_clear_bought() -> None:
        db = SessionLocal()
        try:
            start.wait()
            clear_bought_lines(db, DEADLOCK_OWNER, [(vid, 1) for vid in variant_ids])
            db.commit()
        except Exception as error:  # noqa: BLE001
            errors.append(f"clear_bought_lines: {type(error).__name__}: {str(error)[:80]}")
        finally:
            db.close()

    # Run it a few times - a deadlock is a race and may not hit on the first go.
    for _ in range(30):
        threads = [threading.Thread(target=do_clear_cart), threading.Thread(target=do_clear_bought)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seed = SessionLocal()
        try:
            for vid in variant_ids:
                set_line(seed, DEADLOCK_OWNER, vid, 3)
            seed.commit()
        finally:
            seed.close()

    ok = not errors
    print(f"  clear_cart vs clear_bought_lines: {len(errors)} error(s) over 30 rounds (want 0)")
    for e in errors[:5]:
        print(f"    {e}")
    print(f"  {'PASS' if ok else 'FAIL - lock-order deadlock between the two paths'}")
    _cleanup(DEADLOCK_OWNER)
    return ok


def main(writers: int) -> int:
    variant_id, label = _pick_variant()
    if variant_id is None:
        print("no product variants in the database; seed one first")
        return 2

    # A few distinct variants for the lock-order race.
    setup = SessionLocal()
    try:
        variant_ids = [v.id for v in setup.scalars(select(ProductVariant).limit(4)).all()]
    finally:
        setup.close()

    print(f"variant {label}: {writers} writers racing")
    ok_merge = race_merge(variant_id, writers)
    ok_clear = race_clear(variant_id, writers)
    ok_deadlock = (
        race_clear_vs_bought(variant_ids)
        if len(variant_ids) >= 2
        else (print("  clear_cart vs clear_bought_lines: skipped, need 2+ variants") or True)
    )

    print(f"\n{'PASS' if ok_merge and ok_clear and ok_deadlock else 'FAIL'}")
    return 0 if ok_merge and ok_clear and ok_deadlock else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8))
