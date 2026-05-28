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

        total = Decimal("0")
        for cart_item in payload.cart:
            variant = variants.get(cart_item.variant_id)
            if variant is None:
                raise ValueError(f"Variant {cart_item.variant_id} not found")
            total += variant.price * cart_item.quantity

        amount_cents = int(total * 100)
        pi = create_payment_intent(amount_cents, order_id="pending")

        order = create_order(
            db=db,
            user_id=current_user["sub"],
            cart=payload.cart,
            shipping=payload.shipping,
            stripe_pi_id=pi.id,
        )
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return order
