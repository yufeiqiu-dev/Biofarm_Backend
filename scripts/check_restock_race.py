"""Prove that a restock and a sale landing together keep both.

**Not part of the test suite, and it cannot be.** `app/tests/` runs on in-memory
SQLite, which has no `SELECT ... FOR UPDATE` and silently ignores the clause, so
a concurrency test there passes whether or not the locking works. The suite
asserts the *arithmetic* - that a restock adds rather than sets - which is the
best it can do there and is not the same as knowing the lock holds.

The scenario is the one that made the endpoint necessary. Stock used to be a
field on the product form, which is a read-modify-write over a whole product:
the page reads every field, the admin edits one, and it writes them all back.
The count in that payload was read before they started typing, so a sale made
while the page was open was overwritten by it - shelf 4, customer buys 2, admin
corrects a typo in the description, system says 4 and the shelf holds 2.

A delta cannot lose that write on its own, because the count is never read into
the client. What this proves is the other half: that the read and the write are
inside a held row lock. Drop `with_for_update` and this reports 8 where it wants
4 - the delivery is applied to a count read before four sales committed, and the
sales are gone.

It does not currently distinguish `populate_existing=True` on that `db.get`, and
that is worth stating plainly rather than implying coverage. The flag matters
when the object was already loaded in the same session before the lock was
taken, which is exactly `create_order`'s shape and why its absence there cost
six orders for one unit with the suite green. `adjust_variant_stock` is called
with a request session that has not seen the variant, so `db.get` loads it fresh
either way. The flag stays because the next caller may not be so lucky, and its
absence looks like nothing.

Usage - needs the local Postgres up and `.env` pointing at it:

    PYTHONPATH=. .venv/Scripts/python.exe scripts/check_restock_race.py [buyers]

It picks a variant, sets its stock to `buyers`, then races `buyers` purchases of
one unit each against a single restock of `buyers`. Whatever the interleaving,
every buyer is served and the final count must be exactly `buyers` - one taken
and one added per buyer. It restores the original stock and deletes the orders
it made. Safe against a development database; do not point it at one you care
about.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid

from sqlalchemy import delete, event, select
from sqlalchemy.engine import Engine

from app.db.session import SessionLocal
from app.models.order import Order
from app.models.product_variant import ProductVariant
from app.schemas.order import CartItemIn, ShippingIn
from app.services.order_service import create_order
from app.services.product_service import adjust_variant_stock

RACE_USER_PREFIX = "restock-race-"

SHIPPING = ShippingIn(
    name="Restock Check",
    phone="5551234567",
    address1="1 Test St",
    city="Springfield",
    state="IL",
    zip="62701",
)

# Matches the engine's pool_size + max_overflow. More threads than this cannot
# all hold a connection at once, so they cannot all race - and a thread that
# blocks on checkout would be silently excluded from the verdict.
ENGINE_POOL_CAPACITY = 10

# How long the restock thread dawdles between reading the row and writing it
# back.
#
# The pause is the whole point, and without it this probe is worthless: it
# passed with the row lock deleted entirely, because the read and the write sat
# microseconds apart and no sale could land between them. Real requests are not
# that tight - a session flush, a network hop, a busy worker - and the window is
# the bug. This makes the window big enough to lose a write through, so the lock
# has something to prevent.
RESTOCK_THINKING_SECONDS = 0.4

# Only the restock thread dawdles; the buyers must be free to commit into its
# window.
_dawdle = threading.local()


@event.listens_for(Engine, "after_cursor_execute")
def _pause_after_reading_the_variant(conn, cursor, statement, params, context, many):
    if not getattr(_dawdle, "armed", False):
        return
    if "FROM product_variants" in statement:
        time.sleep(RESTOCK_THINKING_SECONDS)


def main(buyers: int) -> int:
    if buyers + 1 > ENGINE_POOL_CAPACITY:
        print(
            f"  refusing: {buyers} buyers plus the restocker exceeds the pool "
            f"({ENGINE_POOL_CAPACITY}); threads would block on checkout and be "
            "silently excluded"
        )
        return 2

    setup = SessionLocal()
    variant = setup.scalars(select(ProductVariant).limit(1)).first()
    if variant is None:
        print("no product variants in the database; seed one first")
        setup.close()
        return 2

    variant_id = variant.id
    product_id = variant.product_id
    original_stock = variant.stock
    label = variant.catalog_id
    variant.stock = buyers
    setup.commit()
    setup.close()

    print(
        f"variant {label}: stock set to {buyers}, {buyers} buyers taking one each "
        f"while a delivery of {buyers} lands"
    )

    errors: list[str] = []
    reached: list[str] = []
    # Released together, so the threads contend rather than queue politely.
    start = threading.Barrier(buyers + 1)

    def buy(tag: str) -> None:
        db = SessionLocal()
        try:
            start.wait()
            reached.append(tag)
            create_order(
                db,
                user_id=f"{RACE_USER_PREFIX}{tag}",
                cart=[CartItemIn(variant_id=variant_id, quantity=1)],
                shipping=SHIPPING,
                stripe_pi_id=f"pi_restock_race_{uuid.uuid4().hex[:12]}",
                customer_email="race@example.com",
            )
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            errors.append(tag)
            print(f"  buyer {tag}: {type(error).__name__}: {str(error)[:60]}")
        finally:
            db.close()

    def restock() -> None:
        db = SessionLocal()
        try:
            start.wait()
            reached.append("delivery")
            _dawdle.armed = True
            adjust_variant_stock(db, product_id, variant_id, buyers)
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            errors.append("delivery")
            print(f"  delivery: {type(error).__name__}: {str(error)[:60]}")
        finally:
            _dawdle.armed = False
            db.close()

    threads = [threading.Thread(target=buy, args=(str(n),)) for n in range(buyers)]
    threads.append(threading.Thread(target=restock))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check = SessionLocal()
    final_stock = check.get(ProductVariant, variant_id).stock
    orders = list(
        check.scalars(
            select(Order).where(Order.user_id.like(f"{RACE_USER_PREFIX}%"))
        )
    )

    # One unit taken and one added per buyer, so the count comes back to where
    # it started whatever the interleaving. A lost update shows up here as a
    # short count - the delivery applied to a number read before the sales, or
    # the sales applied to a number read before the delivery.
    expected = buyers
    ok = (
        len(reached) == buyers + 1
        and not errors
        and len(orders) == buyers
        and final_stock == expected
    )

    print(f"\n  threads that ran : {len(reached)}/{buyers + 1}")
    print(f"  errored          : {len(errors)}")
    print(f"  orders placed    : {len(orders)} (want {buyers})")
    print(f"  final stock      : {final_stock} (want {expected})")
    if ok:
        print("  PASS")
    elif final_stock < expected:
        print("  FAIL - a write was lost; someone read the count before waiting")
    elif final_stock > expected:
        print(
            "  FAIL - the sales were overwritten; the delivery was applied to a "
            "count read before they committed"
        )
    else:
        print("  FAIL - not every thread raced, so nothing was proved")

    # Through cancel-free cleanup: these orders never existed as far as the shop
    # is concerned, and the stock is restored explicitly below.
    check.execute(delete(Order).where(Order.user_id.like(f"{RACE_USER_PREFIX}%")))
    check.get(ProductVariant, variant_id).stock = original_stock
    check.commit()
    check.close()
    print(f"  (stock restored to {original_stock}, {len(orders)} test orders removed)")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 4))
