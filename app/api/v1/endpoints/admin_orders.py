import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_admin
from app.models.order import OrderStatus
from app.models.product_variant import ProductVariant
from app.schemas.order import AdminOrderItemOut, AdminOrderOut, UpdateOrderStatusRequest, UpdateTrackingRequest
from app.services.order_service import (
    cancel_order,
    confirm_order_admin,
    deliver_order,
    get_order_by_id,
    list_all_orders,
    ship_order,
    update_tracking_number,
)
from app.services.stripe_service import cancel_payment_intent, capture_payment_intent, create_refund

router = APIRouter(prefix="/admin/orders", tags=["admin-orders"])


def _enrich_items_with_stock(order, db: Session):
    from sqlalchemy import select

    variant_ids = [item.variant_id for item in order.items if item.variant_id]
    stock_map: dict = {}
    if variant_ids:
        rows = db.execute(
            select(ProductVariant.id, ProductVariant.stock).where(ProductVariant.id.in_(variant_ids))
        ).all()
        stock_map = {row.id: row.stock for row in rows}

    return [
        AdminOrderItemOut(
            id=item.id,
            variant_id=item.variant_id,
            product_name=item.product_name,
            variant_label=item.variant_label,
            unit_price=item.unit_price,
            quantity=item.quantity,
            current_stock=stock_map.get(item.variant_id) if item.variant_id else None,
        )
        for item in order.items
    ]


def _build_admin_order_out(order, db: Session) -> AdminOrderOut:
    items = _enrich_items_with_stock(order, db)
    return AdminOrderOut(
        id=order.id,
        order_number=order.order_number,
        user_id=order.user_id,
        customer_email=order.customer_email,
        card_brand=order.card_brand,
        card_last4=order.card_last4,
        status=order.status,
        stripe_payment_intent_id=order.stripe_payment_intent_id,
        total_amount=order.total_amount,
        tax_amount=order.tax_amount,
        shipping_name=order.shipping_name,
        shipping_phone=order.shipping_phone,
        shipping_address1=order.shipping_address1,
        shipping_address2=order.shipping_address2,
        shipping_city=order.shipping_city,
        shipping_state=order.shipping_state,
        shipping_zip=order.shipping_zip,
        notes=order.notes,
        tracking_number=order.tracking_number,
        created_at=order.created_at,
        updated_at=order.updated_at,
        items=items,
    )


@router.get("", response_model=list[AdminOrderOut])
def list_orders(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    order_status = None
    if status:
        try:
            order_status = OrderStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    orders = list_all_orders(db, order_status)
    return [_build_admin_order_out(o, db) for o in orders]


@router.get("/{order_id}", response_model=AdminOrderOut)
def get_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    order = get_order_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return _build_admin_order_out(order, db)


@router.patch("/{order_id}/status", response_model=AdminOrderOut)
def update_order_status(
    order_id: uuid.UUID,
    payload: UpdateOrderStatusRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    order = get_order_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    try:
        if payload.status == "confirmed":
            order = confirm_order_admin(db, order_id)
        elif payload.status == "shipped":
            order = ship_order(db, order_id, tracking_number=payload.tracking_number)
            try:
                capture_payment_intent(order.stripe_payment_intent_id)
            except Exception as e:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Stripe capture failed: {e}")
        elif payload.status == "delivered":
            order = deliver_order(db, order_id)
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid status: {payload.status}")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return _build_admin_order_out(order, db)


@router.patch("/{order_id}/tracking", response_model=AdminOrderOut)
def update_tracking(
    order_id: uuid.UUID,
    payload: UpdateTrackingRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    try:
        order = update_tracking_number(db, order_id, payload.tracking_number)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return _build_admin_order_out(order, db)


@router.post("/{order_id}/cancel", response_model=AdminOrderOut)
def cancel_order_endpoint(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    order = get_order_by_id(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.status == OrderStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order is already cancelled"
        )

    try:
        # Capture happens at ship time — anything before shipped has no charge, just void the auth.
        # shipped/delivered means money was already captured, so issue a refund.
        if order.status in (OrderStatus.pending, OrderStatus.awaiting_fulfillment, OrderStatus.confirmed):
            cancel_payment_intent(order.stripe_payment_intent_id)
        else:
            create_refund(order.stripe_payment_intent_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Stripe operation failed: {e}")

    try:
        order = cancel_order(db, order_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return _build_admin_order_out(order, db)
