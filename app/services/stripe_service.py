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
    money moves at ship time. See order_service for the rest of that lifecycle.

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


def calculate_tax(line_items: list[dict], address: dict) -> TaxResult:
    """Calculate tax via Stripe Tax. line_items: [{amount (cents), reference, tax_code}].
    In bypass mode, mocks 8.75% flat rate."""
    settings = get_settings()
    subtotal = sum(item["amount"] for item in line_items)
    if settings.stripe_bypass:
        tax = round(subtotal * 0.0875)
        return TaxResult(tax_amount_cents=tax, total_cents=subtotal + tax)
    s = _get_stripe()
    calc = s.tax.Calculation.create(
        currency="usd",
        line_items=line_items,
        customer_details={"address": address, "address_source": "shipping"},
    )
    return TaxResult(
        tax_amount_cents=calc.tax_amount_exclusive,
        total_cents=calc.amount_total,
    )


def capture_payment_intent(payment_intent_id: str) -> dict | stripe.PaymentIntent:
    settings = get_settings()
    if settings.stripe_bypass:
        return {"id": payment_intent_id, "status": "succeeded"}
    s = _get_stripe()
    return s.PaymentIntent.capture(payment_intent_id)


def cancel_payment_intent(payment_intent_id: str) -> dict | stripe.PaymentIntent:
    settings = get_settings()
    if settings.stripe_bypass:
        return {"id": payment_intent_id, "status": "canceled"}
    s = _get_stripe()
    return s.PaymentIntent.cancel(payment_intent_id)


def create_refund(payment_intent_id: str) -> dict | stripe.Refund:
    settings = get_settings()
    if settings.stripe_bypass:
        return {"id": f"re_bypass_{uuid.uuid4().hex[:16]}", "status": "succeeded"}
    s = _get_stripe()
    return s.Refund.create(payment_intent=payment_intent_id)


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
