"""Application logging.

Every module here takes a `logging.getLogger(__name__)` and then writes to it -
email_service records each send and every failure, stripe_webhook records the
events it handles, cleanup records what it swept. None of it appeared anywhere.

The reason is easy to miss: uvicorn configures its own loggers (`uvicorn`,
`uvicorn.error`, `uvicorn.access`) and leaves the root logger alone. Module
loggers propagate to the root, find no handler, and fall through to the
last-resort handler, which only emits WARNING and above. So `logger.info` was
silently discarded and `logger.exception` arrived without the formatting or
level the rest of the output has.

That matters most where the code deliberately swallows failures. email_service
catches a rejected send, logs it, and carries on by design - so if the log does
not appear, a completely broken email setup is indistinguishable from a working
one.
"""

from __future__ import annotations

import logging
import sys

FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Attach one stdout handler to the root logger, once.

    stdout rather than stderr because App Runner and every other container
    runtime collects it as the service's log stream, and because a log line is
    not an error.

    Idempotent: if something has already configured the root logger - a host,
    a test harness, `pytest --log-cli` - its handler is left alone and only the
    level is applied. Adding a second handler would print every line twice.
    """
    root = logging.getLogger()

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)

    root.setLevel(_level_of(level))


def _level_of(level: str) -> int:
    """A bad value should not stop the service from starting.

    Logging configuration is not worth failing a deploy over, and the setting is
    the kind of thing that gets a typo in a task definition. An unknown value
    falls back to INFO and says so.
    """
    resolved = logging.getLevelNamesMapping().get(level.strip().upper())
    if resolved is None:
        logging.getLogger(__name__).warning(
            "Unknown LOG_LEVEL %r; using INFO", level
        )
        return logging.INFO
    return resolved
