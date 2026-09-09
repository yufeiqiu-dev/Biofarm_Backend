"""Unit tests for the Stripe wrapper.

Every Stripe call in the app goes through app/services/stripe_service.py, and
these exercise it directly with a stubbed client - no database, no endpoint.

They lived at the bottom of test_admin_stats.py, which is where they were
written and nowhere near where anyone would look for them: someone checking
whether the refund logic is covered reads test_admin_orders.py, finds the
endpoint tests, and concludes it is not.
"""

import pytest
from contextlib import contextmanager

def test_a_lapsed_hold_can_still_be_released():
    """Stripe rejects cancelling an intent that is already canceled, and a hold
    lapses on its own after about a week - so an order left long enough could be
    neither voided nor refunded, the endpoint 502'd, and its stock stayed
    reserved with no way out. The dashboard's expired tile pointed straight at
    those orders."""
    import stripe as stripe_sdk
    from unittest.mock import MagicMock, patch as _patch

    from app.services import stripe_service

    already_gone = MagicMock()
    already_gone.status = "canceled"

    client = MagicMock()
    client.PaymentIntent.cancel.side_effect = stripe_sdk.error.InvalidRequestError(
        "already canceled", param=None
    )
    client.PaymentIntent.retrieve.return_value = already_gone

    with _patch.object(stripe_service, "_get_stripe", return_value=client), \
         _patch.object(stripe_service.get_settings(), "stripe_bypass", False):
        result = stripe_service.cancel_payment_intent("pi_lapsed")

    assert result is already_gone, "a hold that has already lapsed is the state we wanted"


def _refund(amount: int, status: str = "succeeded", refund_id: str = "re_1"):
    from unittest.mock import MagicMock

    refund = MagicMock()
    refund.amount = amount
    refund.status = status
    refund.id = refund_id
    return refund


def _stripe_client(charged: int, existing: list, create_error=None):
    from unittest.mock import MagicMock

    client = MagicMock()
    intent = MagicMock()
    intent.amount_received = charged
    intent.amount = charged
    client.PaymentIntent.retrieve.return_value = intent

    listing = MagicMock()
    listing.data = existing
    listing.has_more = False
    client.Refund.list.return_value = listing

    if create_error is not None:
        client.Refund.create.side_effect = create_error
    return client


@contextmanager
def _refunding(client):
    """create_refund against a stubbed Stripe, with bypass off."""
    from unittest.mock import patch as _patch

    from app.services import stripe_service

    with _patch.object(stripe_service, "_get_stripe", return_value=client), \
         _patch.object(stripe_service.get_settings(), "stripe_bypass", False):
        yield


def _rejection(code: str):
    import stripe as stripe_sdk

    error = stripe_sdk.error.InvalidRequestError(code, param=None)
    error.code = code
    return error


def test_a_refund_that_already_happened_does_not_wedge_the_cancellation():
    """The retry after a lost response must not 502 forever.

    Stripe creates the refund, the response is lost to a timeout, and the
    endpoint 502s before cancel_order runs - so the order is still confirmed and
    still holding its stock. Retrying finds captured_at still set and calls
    create_refund again. Left to raise, that order can never be cancelled and
    its stock never comes back.

    The attempt is what discovers it: Stripe rejects a second full refund, and
    the handler confirms the money really is back before reporting success.
    """
    from app.services import stripe_service

    original = _refund(5000)
    client = _stripe_client(
        charged=5000,
        existing=[original],
        create_error=_rejection("charge_already_refunded"),
    )

    with _refunding(client):
        result = stripe_service.create_refund("pi_already_refunded")

    assert result is original, "money already returned is the state the caller wanted"


def test_a_partial_refund_is_not_treated_as_a_completed_one():
    """Otherwise a partial refund issued from the Stripe Dashboard would let an
    unrelated failure report success - the console cancels the order and returns
    its stock while the customer got back part of their money, with nothing
    logged."""
    import stripe as stripe_sdk

    from app.services import stripe_service

    client = _stripe_client(
        charged=5000,
        existing=[_refund(1000)],
        create_error=_rejection("balance_insufficient"),
    )

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.create_refund("pi_partly_refunded")


def test_a_failed_refund_does_not_count_as_money_returned():
    """Status, not just amount. A refund can be created and then fail - the
    funds never leave. Counting one of those as settled would report the
    cancellation as done, mark the order cancelled and return its stock, with
    the customer's money still ours and nothing saying so."""
    import stripe as stripe_sdk

    from app.services import stripe_service

    client = _stripe_client(
        charged=5000,
        existing=[_refund(5000, status="failed")],
        create_error=_rejection("charge_already_refunded"),
    )

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.create_refund("pi_refund_failed")


def test_a_refund_rejected_for_any_other_reason_still_raises():
    """Only *already refunded* is success. Swallowing the rest would report a
    cancellation that returned no money - a disputed charge or an insufficient
    balance is a real failure."""
    import stripe as stripe_sdk

    from app.services import stripe_service

    client = _stripe_client(
        charged=5000, existing=[], create_error=_rejection("balance_insufficient")
    )

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.create_refund("pi_broken")


def test_already_refunded_without_a_matching_refund_still_raises():
    """The claim has to be checkable. Trusting the error code alone would report
    success on an intent carrying no refund that covers the charge."""
    import stripe as stripe_sdk

    from app.services import stripe_service

    client = _stripe_client(
        charged=5000, existing=[], create_error=_rejection("charge_already_refunded")
    )

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.create_refund("pi_claims_refunded")


def test_a_refund_completed_by_someone_else_is_success_whatever_stripe_says():
    """Another actor refunded it; our attempt then fails for its own reason.

    The customer has been made whole, so cancelling the order is now correct.
    Gating on code == "charge_already_refunded" would 502 instead and leave the
    stock held after the money had gone back.
    """
    from app.services import stripe_service

    theirs = _refund(5000)
    client = _stripe_client(
        charged=5000, existing=[theirs], create_error=_rejection("balance_insufficient")
    )

    with _refunding(client):
        assert stripe_service.create_refund("pi_raced") is theirs


def test_a_refund_sends_no_idempotency_key():
    """Deliberately, and it is worth pinning because a fixed key is the obvious
    thing to reach for here.

    Stripe caches a key's response for 24 hours, errors included. One transient
    failure would then be replayed to every retry for a day and the order could
    not be cancelled at all - the same stuck state the key would have been added
    to prevent. Asking what Stripe actually holds has no such failure mode, and
    the caller's row lock is what serialises concurrent attempts.
    """
    from app.services import stripe_service

    client = _stripe_client(charged=5000, existing=[])

    with _refunding(client):
        stripe_service.create_refund("pi_abc123")

    assert "idempotency_key" not in client.Refund.create.call_args.kwargs


def _intent(intent_status: str):
    from unittest.mock import MagicMock

    intent = MagicMock()
    intent.status = intent_status
    return intent


def test_voiding_a_captured_intent_reports_that_it_was_captured():
    """Not just "the void failed".

    release_funds attempts the void without asking Stripe first, so this
    rejection is the only signal that the money already moved - a legacy row,
    or a confirm whose capture succeeded and whose commit rolled back. Lumped in
    with every other InvalidRequestError it raises, the endpoint 502s, and an
    order that should have been refunded is stuck instead.
    """
    from unittest.mock import MagicMock, patch as _patch

    from app.services import stripe_service
    from app.services.stripe_service import PaymentAlreadyCaptured

    client = MagicMock()
    client.PaymentIntent.cancel.side_effect = _rejection("payment_intent_unexpected_state")
    client.PaymentIntent.retrieve.return_value = _intent("succeeded")

    with _refunding(client):
        with pytest.raises(PaymentAlreadyCaptured):
            stripe_service.cancel_payment_intent("pi_captured")


def test_a_void_rejected_in_any_other_state_still_raises():
    """A hold that is neither gone nor captured is a real failure. Reporting it
    as either would leave the money where it is and say otherwise."""
    import stripe as stripe_sdk
    from unittest.mock import MagicMock

    from app.services import stripe_service

    client = MagicMock()
    client.PaymentIntent.cancel.side_effect = _rejection("api_error")
    client.PaymentIntent.retrieve.return_value = _intent("requires_action")

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.cancel_payment_intent("pi_odd")


def test_capturing_an_already_captured_intent_is_not_an_error():
    """The no-double-charge guarantee, now that confirm just attempts it.

    Confirm captures and then commits; a rolled-back commit leaves the money
    taken with captured_at still NULL, so the next attempt tries again. Stripe
    rejects it, and that rejection is the signal - the alternative was a
    retrieve before every confirm, a round trip inside the row lock that tells
    you nothing on the pass that matters and 502s a good confirm when it blips.
    """
    from unittest.mock import MagicMock

    from app.services import stripe_service

    client = MagicMock()
    client.PaymentIntent.capture.side_effect = _rejection("payment_intent_unexpected_state")
    client.PaymentIntent.retrieve.return_value = _intent("succeeded")

    with _refunding(client):
        result = stripe_service.capture_payment_intent("pi_already_captured")

    assert getattr(result, "status", None) == "succeeded"


def test_a_capture_rejected_in_any_other_state_still_raises():
    """An intent that is not captured and will not capture is a real failure.
    Reporting it as success would confirm an order with no money behind it."""
    import stripe as stripe_sdk
    from unittest.mock import MagicMock

    from app.services import stripe_service

    client = MagicMock()
    client.PaymentIntent.capture.side_effect = _rejection("payment_intent_unexpected_state")
    client.PaymentIntent.retrieve.return_value = _intent("canceled")

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.capture_payment_intent("pi_lapsed")


def test_the_refund_lookup_reads_every_page():
    """The check sums refunds to decide whether the charge is fully back, so a
    truncated list understates the total.

    A charge settled by many partial refunds then reads as incomplete and the
    cancellation 502s with the money already returned. Raising the page size is
    not the same as reading the list: Stripe caps a page at 100 and says so with
    has_more.
    """
    from unittest.mock import MagicMock

    from app.services import stripe_service

    first, second = MagicMock(), MagicMock()
    first.data = [_refund(2000, refund_id="re_a")]
    first.has_more = True
    second.data = [_refund(3000, refund_id="re_b")]
    second.has_more = False

    client = _stripe_client(
        charged=5000, existing=[], create_error=_rejection("charge_already_refunded")
    )
    client.Refund.list.side_effect = [first, second]

    with _refunding(client):
        # 2000 + 3000 covers the 5000 charge only if both pages were read.
        stripe_service.create_refund("pi_many_refunds")

    assert client.Refund.list.call_count == 2
    assert client.Refund.list.call_args.kwargs.get("starting_after") == "re_a"


def test_a_failing_lookup_reports_what_stripe_actually_refused():
    """The lookup inside the handler is another network call.

    Letting its failure propagate replaces Stripe's real refusal - the thing
    worth reading in the log - with a connection error, and denies release_funds
    the PaymentAlreadyCaptured it needs to fall back to a refund.
    """
    import stripe as stripe_sdk
    from unittest.mock import MagicMock

    from app.services import stripe_service

    client = MagicMock()
    client.PaymentIntent.cancel.side_effect = _rejection("payment_intent_unexpected_state")
    client.PaymentIntent.retrieve.side_effect = stripe_sdk.error.APIConnectionError("down")

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.cancel_payment_intent("pi_lookup_down")


def test_a_failing_lookup_after_a_capture_refusal_does_the_same():
    import stripe as stripe_sdk
    from unittest.mock import MagicMock

    from app.services import stripe_service

    client = MagicMock()
    client.PaymentIntent.capture.side_effect = _rejection("payment_intent_unexpected_state")
    client.PaymentIntent.retrieve.side_effect = stripe_sdk.error.APIConnectionError("down")

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.capture_payment_intent("pi_lookup_down")


def test_a_failing_refund_lookup_reports_stripe_s_rejection():
    """_completed_refund makes two Stripe calls of its own.

    Letting either failure escape replaces the rejection we are holding -
    charge_already_refunded, the one thing that says the money is already back -
    with a timeout, so the 502 an admin reads names the wrong problem entirely.
    """
    import stripe as stripe_sdk

    from app.services import stripe_service

    client = _stripe_client(
        charged=5000, existing=[], create_error=_rejection("charge_already_refunded")
    )
    client.PaymentIntent.retrieve.side_effect = stripe_sdk.error.APIConnectionError("down")

    with _refunding(client):
        with pytest.raises(stripe_sdk.error.InvalidRequestError):
            stripe_service.create_refund("pi_lookup_down")

