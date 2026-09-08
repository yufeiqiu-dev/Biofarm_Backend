import uuid
from dataclasses import dataclass

import stripe

from app.core.config import get_settings


@dataclass
class MockPaymentIntent:
    id: str
    client_secret: str


@dataclass
class TaxResult:
    tax_amount_cents: int
    total_cents: int


def _get_stripe():
    stripe.api_key = get_settings().stripe_secret_key.get_secret_value()
    return stripe


def create_payment_intent(
    amount_cents: int,
    order_id: str | None = None,
    idempotency_key: str | None = None,
) -> MockPaymentIntent | stripe.PaymentIntent:
    """Authorize (do not capture) amount_cents.

    capture_method="manual" is deliberate: checkout only places a hold, and the
    money moves when an admin confirms the order. Capture used to happen at
    ship, which was the wrong step to hang it on - a hold lapses after about a
    week, and packing cold-chain goods can outrun that, so the failure surfaced
    with the box packed and the stock long since deducted. See order_service for
    the rest of that lifecycle.

    Pass an idempotency_key for anything a client can retry. Without one, a
    double-clicked Pay button or a network-level retry creates a second
    PaymentIntent and a second authorization hold on the customer's card - two
    holds against one basket, and only one of them ever gets voided.
    """
    settings = get_settings()
    if settings.stripe_bypass:
        bypass_id = f"pi_bypass_{uuid.uuid4().hex[:16]}"
        return MockPaymentIntent(id=bypass_id, client_secret=f"{bypass_id}_secret_bypass")
    s = _get_stripe()
    metadata = {"order_id": order_id} if order_id else {}
    kwargs = {"idempotency_key": idempotency_key} if idempotency_key else {}
    return s.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        capture_method="manual",
        metadata=metadata,
        automatic_payment_methods={"enabled": True},
        **kwargs,
    )


def calculate_tax(line_items: list[dict], address: dict, shipping_cents: int = 0) -> TaxResult:
    """Calculate tax via Stripe Tax. line_items: [{amount (cents), reference, tax_code}].
    In bypass mode, mocks 8.75% flat rate.

    Shipping is passed to Stripe rather than added afterwards because most US
    states tax delivery charges, and which ones is exactly the judgement Stripe
    Tax exists to make. The returned total therefore already includes shipping
    and any tax on it - it is what the customer is charged.
    """
    settings = get_settings()
    subtotal = sum(item["amount"] for item in line_items)
    if settings.stripe_bypass:
        # Taxed on goods plus shipping, matching the states that do so - the
        # bypass path must not disagree with the real one about the total.
        tax = round((subtotal + shipping_cents) * 0.0875)
        return TaxResult(
            tax_amount_cents=tax, total_cents=subtotal + shipping_cents + tax
        )
    s = _get_stripe()
    kwargs = {}
    if shipping_cents:
        kwargs["shipping_cost"] = {"amount": shipping_cents}
    calc = s.tax.Calculation.create(
        currency="usd",
        line_items=line_items,
        customer_details={"address": address, "address_source": "shipping"},
        **kwargs,
    )
    return TaxResult(
        tax_amount_cents=calc.tax_amount_exclusive,
        total_cents=calc.amount_total,
    )


def capture_payment_intent(payment_intent_id: str) -> dict | stripe.PaymentIntent:
    """Take the money. An intent already captured counts as success.

    Discovered rather than predicted, the same way cancel_payment_intent treats
    an already-released hold. The callers used to ask first - a
    PaymentIntent.retrieve before every confirm and every legacy
    ship, inside the order's row lock - and that question only ever tells you
    anything on a retry, because a first capture is not captured yet. What it
    cost was a round trip that could fail on its own: a timeout on the retrieve
    502'd a confirm whose capture would have gone through, with the lock held
    for the duration.

    The retry it protects is real, though. Confirm captures and then commits,
    and if that commit rolls back the money is taken with captured_at still
    NULL; the next attempt must not charge a second time. Stripe rejects that,
    which is the signal - so it is caught here instead of pre-empted.
    """
    settings = get_settings()
    if settings.stripe_bypass:
        return {"id": payment_intent_id, "status": "succeeded"}
    s = _get_stripe()
    try:
        return s.PaymentIntent.capture(payment_intent_id)
    except stripe.error.InvalidRequestError as error:
        # As in cancel_payment_intent: a failure here must not replace Stripe's
        # refusal with a connection error.
        try:
            intent = s.PaymentIntent.retrieve(payment_intent_id)
        except Exception:
            raise error
        if getattr(intent, "status", None) == "succeeded":
            return intent
        raise error


def cancel_payment_intent(payment_intent_id: str) -> dict | stripe.PaymentIntent:
    """Release an authorisation. An intent already gone counts as success.

    Stripe rejects cancelling an intent that is already canceled, and an
    authorisation lapses on its own after about a week - so an order left
    unshipped long enough could not be cancelled at all: the void was rejected
    because the hold had gone, and a refund was rejected because nothing had been
    captured. Both branches raised, the endpoint 502'd, and the stock stayed
    reserved with no way out. The dashboard's "card holds expired" tile pointed
    admins straight at those orders.

    An intent that is already released is the state the caller wanted.
    """
    settings = get_settings()
    if settings.stripe_bypass:
        return {"id": payment_intent_id, "status": "canceled"}
    s = _get_stripe()
    try:
        return s.PaymentIntent.cancel(payment_intent_id)
    except stripe.error.InvalidRequestError as error:
        # The lookup is another network call and can fail on its own. Letting
        # that failure propagate replaces Stripe's actual refusal - the thing
        # worth reading in the log - with a connection error, and denies
        # release_funds the PaymentAlreadyCaptured it needs to fall back to a
        # refund. Raising the original keeps the diagnosis and leaves the retry
        # to correct it.
        try:
            intent = s.PaymentIntent.retrieve(payment_intent_id)
        except Exception:
            raise error
        intent_status = getattr(intent, "status", None)
        if intent_status == "canceled":
            return intent
        if intent_status == "succeeded":
            # Distinguished rather than lumped in with every other rejection,
            # because there is a correct action for it and the caller cannot
            # always know in advance that it is needed. See release_funds.
            raise PaymentAlreadyCaptured(payment_intent_id) from error
        raise error


class PaymentAlreadyCaptured(Exception):
    """The hold cannot be voided: the money has already been taken."""


def release_funds(payment_intent_id: str, *, known_captured: bool):
    """Undo a payment, at whatever stage it has reached. Void, or refund.

    The stage is discovered rather than predicted. Both cancel paths used to
    ask Stripe up front - a live PaymentIntent.retrieve on every cancellation,
    inside a `try` whose handler is a 502 and while the order's row lock is
    held - so a Stripe blip turned a cancellation that would have succeeded
    into a wedged order with its stock still reserved.

    Predicting it from the order instead is not sound either, and the tempting
    argument for it is wrong: capture happens at confirm, so an order still
    awaiting fulfilment "cannot" have been captured. It can. Confirm captures
    and then commits, and if that commit rolls back the money is gone while the
    row still reads awaiting_fulfillment with captured_at NULL. Legacy rows
    predating the column have the same shape.

    So: trust the record when it says money moved, and otherwise attempt the
    void and let Stripe correct us.

    The cost is one round trip on the ordinary path and up to five on the worst
    one - cancel, retrieve, refund, and if that refund is refused, retrieve and
    list again - all sequential, and all inside the caller's row lock and its
    database connection. That tail only runs for an intent whose money moved
    without us recording it, which is rare by construction; but with a pool of
    ten connections it is worth knowing that a slow Stripe turns concurrent
    cancellations into held locks rather than fast failures.
    """
    if known_captured:
        return create_refund(payment_intent_id)
    try:
        return cancel_payment_intent(payment_intent_id)
    except PaymentAlreadyCaptured:
        return create_refund(payment_intent_id)


def _completed_refund(s, payment_intent_id: str):
    """An existing refund that already returned the whole charge, or None.

    "Whole" matters. A partial refund issued from the Stripe Dashboard is not
    evidence that this cancellation happened - treating any refund on the intent
    as proof of success would mark the order cancelled and return its stock
    while the customer got back part of their money, with nothing logged.
    """
    intent = s.PaymentIntent.retrieve(payment_intent_id)
    charged = getattr(intent, "amount_received", None) or getattr(intent, "amount", 0)
    if not charged:
        return None

    # Every page, not the first one. The check sums refunds to decide whether
    # the charge is fully back, so a truncated list understates the total: a
    # charge settled by many partial refunds from the Dashboard reads as
    # incomplete, and the cancellation 502s with the money already returned -
    # the wedge this handler exists to prevent. limit=100 is Stripe's maximum
    # per page, which raising the limit alone does not make the whole list.
    refunds = []
    starting_after = None
    while True:
        params = {"payment_intent": payment_intent_id, "limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        listing = s.Refund.list(**params)
        page = getattr(listing, "data", None) or []
        refunds.extend(page)
        if not getattr(listing, "has_more", False) or not page:
            break
        starting_after = getattr(page[-1], "id", None)
        if starting_after is None:
            break
    # Pending counts: the money is committed and will land. Failed and canceled
    # ones do not.
    settled = [r for r in refunds if getattr(r, "status", None) in ("succeeded", "pending")]
    if not settled:
        return None
    if sum(getattr(r, "amount", 0) for r in settled) < charged:
        return None
    return settled[0]


def create_refund(payment_intent_id: str) -> dict | stripe.Refund:
    """Give the money back. A refund that already returned it counts as success.

    Same shape as cancel_payment_intent, deliberately: the two money-returning
    branches must not disagree about how a lost response recovers. Without this
    a cancellation could wedge permanently. Stripe creates the refund, the
    response is lost to a timeout, the endpoint 502s before cancel_order runs -
    so the order is still confirmed and still holding its stock. The admin
    retries, captured_at is still set, and a second Refund.create is rejected as
    charge_already_refunded: 502 again, and again, with the stock never
    returned.

    Attempted first and checked only if Stripe refuses, deliberately, and
    without an idempotency key.

    A fixed key looks like the obvious protection and is the wrong tool: Stripe
    caches a key's response for 24 hours, errors included, so one transient
    failure would be replayed to every retry for a day and the order could not
    be cancelled at all - the same stuck state, reached by the mechanism meant
    to prevent it.

    Asking first is the other tempting shape, and it was here for a pass. It
    bought nothing: Stripe rejects a second full refund with
    charge_already_refunded, which the handler below already recognises. What it
    cost was a PaymentIntent.retrieve and a Refund.list before every refund -
    two round trips inside the caller's row lock, each able to fail - so a blip
    on a call that usually only confirmed what the handler would have told us
    anyway 502'd a cancellation and left the stock reserved. Concurrent attempts
    are not the risk here; the caller holds a row lock on the order.
    """
    settings = get_settings()
    if settings.stripe_bypass:
        return {"id": f"re_bypass_{uuid.uuid4().hex[:16]}", "status": "succeeded"}
    s = _get_stripe()

    try:
        return s.Refund.create(payment_intent=payment_intent_id)
    except stripe.error.InvalidRequestError as error:
        # The question is whether the money is back, not which rejection Stripe
        # chose. Gating on code == "charge_already_refunded" first looks like
        # the tighter check and is actually worse: if another actor refunded
        # between our attempt and this handler, the error can be anything at all
        # while the customer has been made whole - and the gate would turn that
        # into a 502 on an order it is now correct to cancel.
        #
        # The amount check is what keeps this honest either way. A partial
        # refund does not satisfy it, so an unrelated failure over a
        # half-refunded charge still raises rather than reporting a
        # cancellation that returned part of the money.
        # Guarded like the other two handlers. _completed_refund makes two
        # Stripe calls of its own, and letting either failure escape replaces
        # the rejection we are holding - charge_already_refunded, the one thing
        # that says the money is back - with a timeout, and skips the bare
        # `raise` below entirely.
        try:
            refund = _completed_refund(s, payment_intent_id)
        except Exception:
            # `raise error`, not a bare `raise`. Inside a nested handler a bare
            # raise re-raises the *inner* exception - the timeout - which is
            # precisely the substitution this guard exists to prevent.
            raise error
        if refund is None:
            raise error
        return refund


def get_card_details(payment_method_id: str) -> tuple[str, str]:
    """Return (brand, last4) for a payment method, or ("", "") if unavailable.

    Lives here rather than in the webhook endpoint so it honours stripe_bypass
    like every other Stripe call, and so the tests keep a single seam to patch.
    Card details are cosmetic - they appear on the order summary - so every
    failure degrades to empty strings rather than blocking order creation.
    """
    if not payment_method_id or not isinstance(payment_method_id, str):
        return "", ""

    settings = get_settings()
    if settings.stripe_bypass:
        return "visa", "4242"

    try:
        pm = _get_stripe().PaymentMethod.retrieve(payment_method_id)
    except Exception:
        return "", ""

    card = getattr(pm, "card", None)
    if not card:
        return "", ""
    return getattr(card, "brand", "") or "", getattr(card, "last4", "") or ""


def verify_webhook_signature(payload: bytes, sig_header: str) -> stripe.Event:
    settings = get_settings()
    if settings.stripe_bypass:
        # In bypass mode, skip signature check — parse payload as JSON event
        import json
        data = json.loads(payload)
        event = stripe.Event.construct_from(data, stripe.api_key)
        return event
    s = _get_stripe()
    return s.Webhook.construct_event(
        payload, sig_header, settings.stripe_webhook_secret.get_secret_value()
    )
