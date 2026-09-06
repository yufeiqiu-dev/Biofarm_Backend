"""That the application's own log lines actually appear.

This exists because they did not. uvicorn configures `uvicorn`, `uvicorn.error`
and `uvicorn.access` and leaves the root logger alone; module loggers propagate
to a root with no handler and fall through to the last-resort handler, which
emits WARNING and above. Every logger.info in the application was discarded.

It matters most where failures are swallowed on purpose. email_service catches a
rejected send, logs it and carries on by design - so with no log line, a
completely broken email setup looks exactly like a working one.
"""

import logging

import pytest

from app.core.logging import configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Logging is global. Put it back, or one test reconfigures the rest."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.setLevel(level)


def _capture() -> tuple[logging.Handler, list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    return Collector(), records


def test_an_info_line_from_a_service_reaches_a_handler():
    root = logging.getLogger()
    root.handlers = []
    configure_logging("INFO")

    handler, records = _capture()
    root.addHandler(handler)

    logging.getLogger("app.services.email_service").info("sent %s", "order 1234567890")

    assert [r.getMessage() for r in records] == ["sent order 1234567890"]


def test_the_root_logger_gets_a_handler_when_it_has_none():
    root = logging.getLogger()
    root.handlers = []

    configure_logging("INFO")

    assert root.handlers, "nothing would be emitted at all"
    assert root.level == logging.INFO


def test_configuring_twice_does_not_duplicate_output():
    """Every line printed twice is its own kind of broken."""
    root = logging.getLogger()
    root.handlers = []

    configure_logging("INFO")
    configure_logging("DEBUG")

    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG, "the level should still be applied"


def test_a_handler_configured_by_the_host_is_left_alone():
    root = logging.getLogger()
    existing = logging.NullHandler()
    root.handlers = [existing]

    configure_logging("INFO")

    assert root.handlers == [existing]


def test_a_nonsense_level_falls_back_instead_of_refusing_to_start():
    """A typo in a task definition is not worth failing a deploy over."""
    root = logging.getLogger()
    root.handlers = []

    configure_logging("VERBOSE-ISH")

    assert root.level == logging.INFO


def test_the_level_is_case_insensitive():
    root = logging.getLogger()
    root.handlers = []

    configure_logging("debug")

    assert root.level == logging.DEBUG
