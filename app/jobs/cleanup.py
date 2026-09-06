"""Sweep checkout sessions that never became orders.

A `CheckoutSession` is written when a PaymentIntent is created and deleted when
the webhook converts it into an order or reports the intent cancelled. Sessions
survive both only when a webhook never arrived - the backend was down, or Stripe
gave up retrying - so they accumulate slowly and harmlessly.

This used to run in the FastAPI lifespan hook, which meant a table scan and
delete on every deploy, restart, and scale-out, at the moment the service was
trying to become healthy. It is daily housekeeping, so it belongs on a schedule:

    python -m app.jobs.cleanup

Run it from EventBridge (or any cron) once a day. Exits non-zero on failure so
a scheduler can alarm on it.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.services.order_service import cleanup_stale_checkout_sessions

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 8


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=(
            "Delete sessions older than this many days (default: "
            f"{DEFAULT_MAX_AGE_DAYS}). Keep it comfortably longer than Stripe's "
            "retry window so a session is never removed while a webhook could "
            "still legitimately arrive for it."
        ),
    )
    args = parser.parse_args(argv)

    # The same configuration the service uses, so raising LOG_LEVEL affects the
    # nightly sweep too. It used to hardcode INFO and ignore the setting.
    configure_logging(get_settings().log_level)

    try:
        with SessionLocal() as db:
            deleted = cleanup_stale_checkout_sessions(db, max_age_days=args.max_age_days)
    except Exception:
        logger.exception("checkout session cleanup failed")
        return 1

    logger.info(
        "checkout session cleanup complete: %d session(s) older than %d days deleted",
        deleted,
        args.max_age_days,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
