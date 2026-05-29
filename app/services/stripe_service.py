import uuid
from dataclasses import dataclass

import stripe

from app.core.config import get_settings


@dataclass
class MockPaymentIntent:
    id: str
    client_secret: str


def _get_stripe():
    stripe.api_key = get_settings().stripe_secret_key
    return stripe


def create_payment_intent(amount_cents: int, order_id: str) -> MockPaymentIntent | stripe.PaymentIntent:
    settings = get_settings()
    if settings.stripe_bypass:
        bypass_id = f"pi_bypass_{uuid.uuid4().hex[:16]}"
        return MockPaymentIntent(id=bypass_id, client_secret=f"{bypass_id}_secret_bypass")
    s = _get_stripe()
    return s.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        capture_method="manual",
        metadata={"order_id": order_id},
        automatic_payment_methods={"enabled": True},
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


def verify_webhook_signature(payload: bytes, sig_header: str) -> stripe.Event:
    settings = get_settings()
    if settings.stripe_bypass:
        # In bypass mode, skip signature check — parse payload as JSON event
        import json
        data = json.loads(payload)
        event = stripe.Event.construct_from(data, stripe.api_key)
        return event
    s = _get_stripe()
    return s.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
