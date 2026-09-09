"""Prove that two admins confirming one order cannot charge the card twice.

**Not part of the test suite, and it cannot be.** `app/tests/` runs on in-memory
SQLite, which ignores `SELECT ... FOR UPDATE`, so a concurrency test there passes
whether or not the locking is correct. The suite asserts that the lock is
*requested*; this asserts that it works.

It earned its place immediately. Before `load_order_for_update` existed, two
simultaneous confirms both read `captured_at IS NULL` and both captured - and
Stripe rejects a second capture, so the admin saw a 502 on an order that had in
fact been charged. With the lock, the second confirm sees the first.

The Stripe call is stood in for rather than made: what is being tested is the
serialisation, not the SDK.

Usage - needs the local Postgres up and `.env` pointing at it:

    PYTHONPATH=. .venv/Scripts/python.exe scripts/check_capture_race.py [admins]

It creates one order, races `admins` threads to confirm it, then deletes what it
made. Safe against a development database; do not point it at anything real.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.order import Order, OrderStatus
from app.models.product_variant import ProductVariant
from app.schemas.order import CartItemIn, ShippingIn
from unittest.mock import patch

from app.services import email_service
from app.services.order_service import (
    cancel_order,
    confirm_order_admin,
    create_order,
    load_order_for_update,
)

PROBE_USER = "capture-race-probe"

# Stands in for the Stripe round trip that happens between reading captured_at
# and writing it. Modest, and enough to make the window real.
STRIPE_ROUND_TRIP_SECONDS = 0.3

# Matches the engine's pool_size + max_overflow. More threads than this cannot
# all hold a connection at once, so they cannot all race.
ENGINE_POOL_CAPACITY = 10

SHIPPING = ShippingIn(
    name="Race Check",
    phone="5551234567",
    address1="1 Test St",
    city="Springfield",
    state="IL",
    zip="62701",
)


def main(admins: int) -> int:
    setup = SessionLocal()
    variant = setup.scalars(
        select(ProductVariant).where(ProductVariant.stock >= 1).limit(1)
    ).first()
    if variant is None:
        print("no variant with stock; seed one first")
        setup.close()
        return 2

    order = create_order(
        db=setup,
        user_id=PROBE_USER,
        cart=[CartItemIn(variant_id=variant.id, quantity=1)],
        shipping=SHIPPING,
        stripe_pi_id=f"pi_capture_race_{uuid.uuid4().hex[:8]}",
    )
    order.status = OrderStatus.awaiting_fulfillment
    setup.commit()
    order_id = order.id
    setup.close()

    print(f"order {order_id}: {admins} admins confirming at once")

    # The connection pool bounds how many admins can genuinely race. A thread
    # that cannot check out a connection blocks, times out, and is excluded -
    # and the verdict below counted only captures, so the script cheerfully
    # printed PASS while proving serialisation across far fewer admins than it
    # claimed.
    capacity = ENGINE_POOL_CAPACITY
    if admins > capacity:
        print(f"  refusing: {admins} admins exceeds the pool ({capacity}); "
              f"threads would block on checkout and be silently excluded")
        return 2

    captures: list[str] = []
    reached: list[str] = []
    errors: list[str] = []
    start = threading.Barrier(admins)

    def confirm(tag: str) -> None:
        db = SessionLocal()
        try:
            start.wait()
            # Recorded before the lock is attempted, so a thread that blocks on
            # connection checkout is visible in the verdict rather than silently
            # excluded from it.
            reached.append(tag)

            # Mirrors the endpoint: lock, check the fact, capture, record it,
            # move the status - all in one transaction, so the lock spans the
            # whole thing.
            locked = load_order_for_update(db, order_id)
            if locked is not None and locked.captured_at is None:
                # Stands in for capture_payment_intent, *including its latency*.
                #
                # The pause is the whole point. Production calls Stripe between
                # reading captured_at and recording it - a network round trip of
                # a few hundred milliseconds - and that gap is the race. Without
                # it the probe closed the window in microseconds and passed even
                # with the lock removed, which is worse than having no probe.
                captures.append(tag)
                time.sleep(STRIPE_ROUND_TRIP_SECONDS)
                # Set, not committed - matching the endpoint. The probe used to
                # commit here, which is the shape that was *removed* for
                # dropping the row lock between the capture and the status
                # change. It still printed PASS, so the one artifact meant to
                # prove the locking was validating the design the locking
                # forbids.
                locked.captured_at = datetime.now(tz=timezone.utc)
                confirm_order_admin(db, order_id)
        except Exception as error:  # noqa: BLE001 - reporting, not handling
            errors.append(tag)
            print(f"  admin {tag}: {type(error).__name__}: {str(error)[:60]}")
        finally:
            db.close()

    threads = [threading.Thread(target=confirm, args=(str(i),)) for i in range(admins)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Every thread must have run, none may have errored, and exactly one may
    # have captured. Checking only the last of those let a run where half the
    # threads never started report success - and a run where the capturing
    # thread died report "charged more than once", the opposite diagnosis.
    ok = len(reached) == admins and not errors and len(captures) == 1

    print(f"\n  admins that ran : {len(reached)}/{admins}")
    print(f"  errored         : {len(errors)}")
    print(f"  captures        : {len(captures)} (want exactly 1)")
    if ok:
        print("  PASS")
    elif len(captures) > 1:
        print("  FAIL - the customer was charged more than once")
    elif len(captures) == 0:
        print("  FAIL - nobody captured; the order was not confirmed at all")
    else:
        print("  FAIL - not every admin raced, so nothing was proved")

    # Through cancel_order, not a bare delete. create_order deducts stock, so
    # deleting the row left a real variant permanently one unit short on every
    # run - against a database this file calls safe to point it at.
    cleanup = SessionLocal()
    for stray in cleanup.scalars(
        select(Order).where(Order.user_id == PROBE_USER)
    ).all():
        if stray.status != OrderStatus.cancelled:
            with patch.object(email_service, "send_order_cancelled"):
                cancel_order(cleanup, stray.id)
    cleanup.execute(delete(Order).where(Order.user_id == PROBE_USER))
    cleanup.commit()
    cleanup.close()
    print("  cleaned up, stock returned")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 4))
