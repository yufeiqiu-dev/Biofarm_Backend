import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.order_service import create_order_from_checkout_session, delete_checkout_session
from app.services.stripe_service import verify_webhook_signature

router = APIRouter(prefix="/stripe", tags=["stripe"])


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook_signature(payload, sig_header)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    if event.type == "payment_intent.amount_capturable_updated":
        # Customer authorized the card — PI is at requires_capture, ready for admin to confirm.
        # This is the correct event for capture_method=manual.
        create_order_from_checkout_session(db, event.data.object.id)
    elif event.type == "payment_intent.succeeded":
        # Fallback: fires after capture for some payment methods. Idempotent — no-op if order already exists.
        create_order_from_checkout_session(db, event.data.object.id)
    elif event.type == "payment_intent.canceled":
        delete_checkout_session(db, event.data.object.id)

    return {"status": "ok"}
