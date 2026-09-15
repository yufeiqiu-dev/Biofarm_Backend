"""Sweep checkout sessions and cart tombstones that have outlived their use.

A `CheckoutSession` is written when a PaymentIntent is created and deleted when
the webhook converts it into an order or reports the intent cancelled. Sessions
survive both only when a webhook never arrived - the backend was down, or Stripe
gave up retrying - so they accumulate slowly and harmlessly.

A cart line's tombstone (`deleted_at` set) is kept around so a device that has
been offline can be told "this was actually deleted" instead of resurrecting it
on its next sync - see
Biofarm_KnowledgeBase/documentation/designs/2026-09-08-local-first-cart-sync.md.
Once no plausible offline device could still be carrying a pre-deletion copy,
the row serves no purpose.

Both used to run in the FastAPI lifespan hook, which meant a table scan and
delete on every deploy, restart, and scale-out, at the moment the service was
trying to become healthy. Both are daily housekeeping, so they belong on a
schedule instead:

    python -m app.jobs.cleanup

Run it from EventBridge (or any cron) once a day. Exits non-zero if either sweep
fails, so a scheduler can alarm on it - but one failing does not skip the other,
since they touch unrelated tables for unrelated reasons.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.services.cart_service import TOMBSTONE_MAX_AGE_DAYS, sweep_cart_tombstones
from app.services.order_service import cleanup_stale_checkout_sessions

logger = logging.getLogger(__name__)

DEFAULT_SESSION_MAX_AGE_DAYS = 8


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_SESSION_MAX_AGE_DAYS,
        help=(
            "Delete checkout sessions older than this many days (default: "
            f"{DEFAULT_SESSION_MAX_AGE_DAYS}). Keep it comfortably longer than "
            "Stripe's retry window so a session is never removed while a "
            "webhook could still legitimately arrive for it."
        ),
    )
    parser.add_argument(
        "--cart-tombstone-max-age-days",
        type=int,
        default=TOMBSTONE_MAX_AGE_DAYS,
        help=(
            "Hard-delete cart tombstones older than this many days (default: "
            f"{TOMBSTONE_MAX_AGE_DAYS}). Keep it comfortably longer than any "
            "plausible stretch of a device staying offline, or a very late "
            "sync can resurrect what it deleted."
        ),
    )
    args = parser.parse_args(argv)

    # The same configuration the service uses, so raising LOG_LEVEL affects the
    # nightly sweep too. It used to hardcode INFO and ignore the setting.
    configure_logging(get_settings().log_level)

    failed = False

    try:
        with SessionLocal() as db:
            deleted = cleanup_stale_checkout_sessions(db, max_age_days=args.max_age_days)
        logger.info(
            "checkout session cleanup complete: %d session(s) older than %d days deleted",
            deleted,
            args.max_age_days,
        )
    except Exception:
        logger.exception("checkout session cleanup failed")
        failed = True

    # A separate try/except and a separate session, deliberately: these two
    # sweeps have nothing to do with each other, and one raising must not
    # prevent the other from running - nor leave it silently unattempted with
    # no line in the log saying so.
    try:
        with SessionLocal() as db:
            swept = sweep_cart_tombstones(db, max_age_days=args.cart_tombstone_max_age_days)
        logger.info(
            "cart tombstone cleanup complete: %d row(s) older than %d days removed",
            swept,
            args.cart_tombstone_max_age_days,
        )
    except Exception:
        logger.exception("cart tombstone cleanup failed")
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
