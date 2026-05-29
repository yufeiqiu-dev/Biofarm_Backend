from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_admin
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product_variant import ProductVariant
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate
from app.services.product_service import (
    create_product,
    delete_product,
    get_product_by_id,
    list_products,
    update_product,
)

router = APIRouter(
    prefix="/admin/products",
    tags=["admin-products"],
)


@router.get("", response_model=list[ProductOut])
def get_products(db: Session = Depends(get_db), current_user=Depends(require_admin),):
    return list_products(db)


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: UUID, db: Session = Depends(get_db), current_user=Depends(require_admin),):
    product = get_product_by_id(db, product_id)
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )
    return product


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def add_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    try:
        product = create_product(db, payload)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    if not product:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create product",
        )
    return product


@router.put("/{product_id}", response_model=ProductOut)
def edit_product(
    product_id: UUID,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    try:
        product = update_product(db, product_id, payload)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cat_id or catalog_id already exists",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_product(
    product_id: UUID,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    product = get_product_by_id(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    variant_ids = [v.id for v in product.variants]
    if variant_ids:
        active_statuses = [
            OrderStatus.pending, OrderStatus.awaiting_fulfillment,
            OrderStatus.confirmed, OrderStatus.shipped,
        ]
        active_count = db.scalar(
            select(func.count()).select_from(OrderItem)
            .join(Order, OrderItem.order_id == Order.id)
            .where(
                OrderItem.variant_id.in_(variant_ids),
                Order.status.in_(active_statuses),
            )
        ) or 0
        if active_count > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot delete: {active_count} active order(s) reference this product. Cancel or complete those orders first.",
            )

    delete_product(db, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)