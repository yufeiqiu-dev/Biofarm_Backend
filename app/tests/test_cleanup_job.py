"""app/jobs/cleanup.py runs two unrelated sweeps in one invocation.

Untested until now - this file exists because that invocation changed shape
when the cart tombstone sweep was added, and "one sweep failing must not skip
the other, nor be silently swallowed" is exactly the kind of thing that looks
right on a read and is not.
"""

from unittest.mock import patch

from app.jobs import cleanup


def test_both_sweeps_run_when_both_succeed():
    with patch.object(cleanup, "cleanup_stale_checkout_sessions", return_value=2) as sessions, \
         patch.object(cleanup, "sweep_cart_tombstones", return_value=5) as tombstones:
        code = cleanup.main([])

    assert code == 0
    sessions.assert_called_once()
    tombstones.assert_called_once()


def test_a_failing_session_sweep_does_not_skip_the_tombstone_sweep():
    with patch.object(cleanup, "cleanup_stale_checkout_sessions", side_effect=RuntimeError("db down")), \
         patch.object(cleanup, "sweep_cart_tombstones", return_value=5) as tombstones:
        code = cleanup.main([])

    assert code == 1, "the failure must be reported, not swallowed"
    assert tombstones.call_count == 1, "the unrelated sweep must still have run"


def test_a_failing_tombstone_sweep_does_not_hide_that_sessions_still_ran():
    with patch.object(cleanup, "cleanup_stale_checkout_sessions", return_value=2) as sessions, \
         patch.object(cleanup, "sweep_cart_tombstones", side_effect=RuntimeError("db down")):
        code = cleanup.main([])

    assert code == 1
    sessions.assert_called_once()


def test_the_cart_tombstone_age_is_configurable():
    """Proves the CLI flag actually reaches the service function rather than
    being parsed and quietly ignored."""
    with patch.object(cleanup, "sweep_cart_tombstones") as sweep:
        cleanup.main(["--cart-tombstone-max-age-days", "30"])

    call = sweep.call_args
    assert call.kwargs.get("max_age_days", call.args[-1] if call.args else None) == 30
