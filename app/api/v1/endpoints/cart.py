import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_user
from app.schemas.cart import CartLineIn, CartLineOut, CartOut
from app.services.cart_service import (
    CartOwner,
    CartLine,
    cart_subtotal,
    clear_cart,
    get_cart,
    remove_line,
    set_line,
)

router = APIRouter(prefix="/cart", tags=["cart"])


def _render(lines: list[CartLine]) -> CartOut:
    items = [
        CartLineOut(
            variant_id=line.variant.id,
            product_id=line.variant.product_id,
            name=line.variant.product.name if line.variant.product else "Unknown",
            catalog_number=line.variant.catalog_id,
            size_label=f"{line.variant.size_value} {line.variant.size_unit}",
            image_url=(
                (line.variant.product.image_urls or [""])[0]
                if line.variant.product
                else ""
            ),
            unit_price=line.variant.price,
            quantity=line.quantity,
            available=line.variant.stock,
            over_stock=line.over_stock,
        )
        for line in lines
    ]
    return CartOut(
        items=items,
        subtotal=cart_subtotal(lines),
        # Named for the customer's benefit rather than the shelf's: what they
        # cannot currently have in full.
        unavailable=[item.catalog_number for item in items if item.over_stock],
    )


@router.get("", response_model=CartOut)
def read_cart(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    """The basket, priced from the catalogue as it stands.

    Kept on the server because a basket in localStorage belongs to a device and
    not to a person - fill one on a phone, sign in on a laptop, and it was gone.
    """
    return _render(get_cart(db, CartOwner.user(current_user["sub"])))


@router.put("/items/{variant_id}", response_model=CartOut)
def put_line(
    variant_id: uuid.UUID,
    payload: CartLineIn,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    """Set one line to a quantity.

    Per line rather than a whole-basket PUT, which would be a read-modify-write
    over something two open tabs can both edit - the same shape that let the
    product form overwrite stock sold while it was open.
    """
    owner = CartOwner.user(current_user["sub"])
    try:
        set_line(db, owner, variant_id, payload.quantity)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return _render(get_cart(db, owner))


@router.delete("/items/{variant_id}", response_model=CartOut)
def delete_line(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    owner = CartOwner.user(current_user["sub"])
    remove_line(db, owner, variant_id)
    return _render(get_cart(db, owner))


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def empty_cart(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    clear_cart(db, CartOwner.user(current_user["sub"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
