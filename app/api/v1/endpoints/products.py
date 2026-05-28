from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.product import ProductOut
from app.services.product_service import get_product_by_id, list_public_products

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=list[ProductOut])
def get_products(
    search: str | None = Query(default=None),
    tags: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
):
    return list_public_products(db, search=search, tags=tags or None)


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: UUID, db: Session = Depends(get_db)):
    product = get_product_by_id(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product
