import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_user
from app.models.product_variant import ProductVariant
from app.schemas.order import CreatePaymentIntentRequest, OrderOut, PaymentIntentResponse
from app.services.order_service import (
    create_order,
    get_order_by_id,
    get_orders_for_user,
)
from app.services.stripe_service import create_payment_intent

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "/payment-intent",
    response_model=PaymentIntentResponse,
    status_code=status.HTTP_201_CREATED,
)
def initiate_checkout(
    payload: CreatePaymentIntentRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    try:
        variant_ids = [item.variant_id for item in payload.cart]
        variants = {
            v.id: v
            for v in db.scalars(select(ProductVariant).where(ProductVariant.id.in_(variant_ids))).all()
        }

        # Validate all variants exist and have sufficient stock
        for cart_item in payload.cart:
            variant = variants.get(cart_item.variant_id)
            if variant is None:
                raise ValueError(f"Variant {cart_item.variant_id} not found")
            if variant.stock < cart_item.quantity:
                raise ValueError(
                    f"Insufficient stock for variant {cart_item.variant_id}: "
                    f"requested {cart_item.quantity}, available {variant.stock}"
                )

        # Create the order first with a placeholder PI ID to avoid orphaned Stripe PIs
        placeholder_pi_id = f"pending_{uuid.uuid4().hex}"
        order = create_order(
            db=db,
            user_id=current_user["sub"],
            cart=payload.cart,
            shipping=payload.shipping,
            stripe_pi_id=placeholder_pi_id,
        )

        # Compute amount from the persisted order total
        amount_cents = int(order.total_amount * 100)

        # Now create the Stripe PI with the real order ID
        pi = create_payment_intent(amount_cents, order_id=str(order.id))

        # Update the order with the real Stripe PI ID
        order.stripe_payment_intent_id = pi.id
        db.commit()
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return PaymentIntentResponse(client_secret=pi.client_secret, order_id=order.id)


@router.get("", response_model=list[OrderOut])
def list_my_orders(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    return get_orders_for_user(db, current_user["sub"])


@router.get("/{order_id}", response_model=OrderOut)
def get_my_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    order = get_order_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.user_id != current_user["sub"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order
