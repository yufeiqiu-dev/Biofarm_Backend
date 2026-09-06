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
from app.services.stripe_service import (
    cancel_payment_intent,
    get_card_details,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stripe", tags=["stripe"])


async def _extract_card_info(pi) -> tuple[str, str]:
    """Return (card_brand, card_last4) for a PaymentIntent, or ("", "").

    The lookup itself lives in stripe_service - calling the SDK from an endpoint
    would sidestep stripe_bypass and remove the seam the tests patch. It is a
    blocking HTTP call, so it goes to a thread rather than stalling the loop.
    """
    return await asyncio.to_thread(get_card_details, getattr(pi, "payment_method", None))


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook_signature(payload, sig_header)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")
    except ValueError:
        # construct_event raises this for a body that is not valid JSON. Letting
        # it escape produced a 500, and a 500 is precisely what tells Stripe to
        # retry - on a payload that will never parse, for the full retry
        # schedule. 400 says "do not bother", which is the truth.
        logger.warning("stripe webhook: unparseable payload, %d bytes", len(payload))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload")

    if event.type in ("payment_intent.amount_capturable_updated", "payment_intent.succeeded"):
        pi = event.data.object
        card_brand, card_last4 = await _extract_card_info(pi)
        try:
            order = create_order_from_checkout_session(
                db, pi.id, card_brand=card_brand, card_last4=card_last4
            )
        except ValueError as exc:
            # Sold out between the customer paying and this webhook arriving.
            # Stock is taken when the order is created, so there is no order and
            # nothing to fulfil - but the card is authorised.
            #
            # Letting this escape would return 500, which is how Stripe is told
            # to retry: it would retry for days, fail identically every time, and
            # leave the authorisation live until it expired on its own. So the
            # authorisation is voided and the event is acknowledged.
            logger.error(
                "webhook %s for payment intent %s: %s - voiding the authorisation",
                event.type, pi.id, exc,
            )
            # Before deleting the session, deliberately. If the void fails this
            # raises, the session survives, and Stripe's retry brings us back
            # here to try again. Deleting first would send the retry down the
            # "no session and no order" path instead, which 500s forever.
            await asyncio.to_thread(cancel_payment_intent, pi.id)
            delete_checkout_session(db, pi.id)
            return {"status": "ok"}

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
