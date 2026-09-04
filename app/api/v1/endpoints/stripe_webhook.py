import asyncio
import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.order_service import (
    create_order_from_checkout_session,
    delete_checkout_session,
    get_order_by_payment_intent,
)
from app.services.stripe_service import verify_webhook_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stripe", tags=["stripe"])


async def _extract_card_info(pi) -> tuple[str, str]:
    """Return (card_brand, card_last4) from a PaymentIntent object, or ("", "")."""
    pm_id = getattr(pi, "payment_method", None)
    if not pm_id or not isinstance(pm_id, str):
        return "", ""
    try:
        pm = await asyncio.to_thread(stripe.PaymentMethod.retrieve, pm_id)
        card = getattr(pm, "card", None)
        if card:
            return getattr(card, "brand", "") or "", getattr(card, "last4", "") or ""
    except Exception:
        pass
    return "", ""


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook_signature(payload, sig_header)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    if event.type in ("payment_intent.amount_capturable_updated", "payment_intent.succeeded"):
        pi = event.data.object
        card_brand, card_last4 = await _extract_card_info(pi)
        order = create_order_from_checkout_session(
            db, pi.id, card_brand=card_brand, card_last4=card_last4
        )

        if order is None:
            # No checkout session matched. Either this intent was already
            # converted - both event types above reach here, and the first one
            # consumes the session - or the session is genuinely gone and a paid
            # intent has no order behind it.
            existing = get_order_by_payment_intent(db, pi.id)
            if existing is not None:
                logger.info(
                    "webhook %s for payment intent %s: order %s already exists, nothing to do",
                    event.type, pi.id, existing.id,
                )
            else:
                # A customer has been charged and no order exists. Returning 200
                # here would tell Stripe this was handled and burn the retry
                # that is the only free recovery mechanism available.
                logger.error(
                    "webhook %s for payment intent %s: no checkout session and no order - "
                    "payment taken with nothing to fulfil",
                    event.type, pi.id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="No checkout session or order for this payment intent",
                )
    elif event.type == "payment_intent.canceled":
        delete_checkout_session(db, event.data.object.id)

    return {"status": "ok"}
