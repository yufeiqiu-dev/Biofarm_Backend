import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.order_service import cancel_order_from_failed_payment, confirm_order
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

    pi_id = event.data.object.id

    if event.type == "payment_intent.succeeded":
        confirm_order(db, pi_id)
    elif event.type == "payment_intent.payment_failed":
        cancel_order_from_failed_payment(db, pi_id)

    return {"status": "ok"}
