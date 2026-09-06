"""Prove that two buyers cannot both take the last unit.

**Not part of the test suite, and it cannot be.** `app/tests/` runs on in-memory
SQLite, which has no `SELECT ... FOR UPDATE` and silently ignores the clause - so
a concurrency test there passes whether or not the locking is correct. The suite
compensates by asserting that the service *asks* for the lock, which is the best
it can do and is not the same as knowing the lock works.

This is the version that knows. It runs real threads against real Postgres,
which is what every deployed environment uses.

It has already earned its place twice. Both times the suite was entirely green:

1. `Session.get(..., with_for_update=True)` locked the row but left the
   already-loaded attributes stale, because `create_order` loads the variants
   before taking stock. Every buyer waited politely for the lock and then
   deducted from the value it had read before waiting. Eight buyers, one unit,
   six orders. Fixed with `populate_existing=True`.

2. Re-run after the fix: one order, every time.

Usage - needs the local Postgres up and `.env` pointing at it:

    PYTHONPATH=. .venv/Scripts/python.exe scripts/check_stock_race.py [buyers]

It picks a variant, forces its stock to 1, races `buyers` threads for it, then
puts the stock back and deletes the orders it made. Safe to run against a
development database; do not point it at anything you care about.
"""

from __future__ import annotations

import sys
import threading
import uuid

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.order import Order
from app.models.product_variant import ProductVariant
from app.schemas.order import CartItemIn, ShippingIn
from app.services.order_service import create_order

RACE_USER_PREFIX = "stock-race-"

SHIPPING = ShippingIn(
    name="Race Check",
    phone="5551234567",
    address1="1 Test St",
    city="Springfield",
    state="IL",
    zip="62701",
)


def main(buyers: int) -> int:
    setup = SessionLocal()
    variant = setup.scalars(select(ProductVariant).limit(1)).first()
    if variant is None:
        print("no product variants in the database; seed one first")
        setup.close()
        return 2

    variant_id, original_stock, label = variant.id, variant.stock, variant.catalog_id
    variant.stock = 1
    setup.commit()
    setup.close()

    print(f"variant {label}: stock forced to 1, {buyers} buyers racing for it")

    results: dict[str, tuple[str, str]] = {}
    # Released together, so the threads contend rather than queue politely.
    start = threading.Barrier(buyers)

    def buy(tag: str) -> None:
        db = SessionLocal()
        try:
            start.wait()
            order = create_order(
                db=db,
                user_id=f"{RACE_USER_PREFIX}{tag}",
                cart=[CartItemIn(variant_id=variant_id, quantity=1)],
                shipping=SHIPPING,
                stripe_pi_id=f"pi_race_{tag}_{uuid.uuid4().hex[:8]}",
            )
            results[tag] = ("ORDER", str(order.order_number))
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            results[tag] = (type(error).__name__, str(error)[:60])
        finally:
            db.close()

    threads = [threading.Thread(target=buy, args=(str(i),)) for i in range(buyers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for tag, (outcome, detail) in sorted(results.items(), key=lambda e: int(e[0])):
        print(f"  buyer {tag}: {outcome:12} {detail}")

    check = SessionLocal()
    final_stock = check.get(ProductVariant, variant_id).stock
    orders = sum(1 for outcome, _ in results.values() if outcome == "ORDER")

    ok = orders == 1 and final_stock == 0
    print(f"\n  orders created : {orders} (want exactly 1)")
    print(f"  stock left     : {final_stock} (want 0, and never below it)")
    print(f"  {'PASS' if ok else 'FAIL - OVERSOLD'}")

    check.execute(
        delete(Order).where(Order.user_id.like(f"{RACE_USER_PREFIX}%"))
    )
    check.get(ProductVariant, variant_id).stock = original_stock
    check.commit()
    check.close()
    print(f"  cleaned up; {label} stock restored to {original_stock}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8))
