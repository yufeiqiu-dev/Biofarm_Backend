import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_user
from app.schemas.cart import (
    CartLineIn,
    CartLineOut,
    CartMergeRequest,
    CartOut,
    CartTombstoneOut,
)
from app.services.cart_service import (
    CartLineMerge,
    CartOwner,
    cart_subtotal,
    clear_cart,
    get_cart,
    get_cart_tombstones,
    merge_cart,
    remove_line,
    set_line,
)

router = APIRouter(prefix="/cart", tags=["cart"])


def _cart_out(db: Session, owner: CartOwner, *, with_tombstones: bool = False) -> CartOut:
    """The basket as one response: live lines priced from the catalogue.

    `with_tombstones` adds the removed lines a pulling device needs to resolve
    deletions made on another device (see get_cart_tombstones). Only GET /cart
    sets it - that is the client's pull path. A PUT sync's response is
    fire-and-forget on the client (it keeps editing locally and reconciles only
    at a pull point), so paying the extra query and payload on every debounced
    sync would be for a body nobody reads. The per-line endpoints are not the
    sync path at all.
    """
    lines = get_cart(db, owner)
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
            client_updated_at=line.client_updated_at,
        )
        for line in lines
    ]
    return CartOut(
        items=items,
        subtotal=cart_subtotal(lines),
        # Named for the customer's benefit rather than the shelf's: what they
        # cannot currently have in full.
        unavailable=[item.catalog_number for item in items if item.over_stock],
        deleted_lines=[
            CartTombstoneOut(
                variant_id=tombstone.variant_id,
                client_updated_at=tombstone.client_updated_at,
            )
            for tombstone in (get_cart_tombstones(db, owner) if with_tombstones else [])
        ],
    )


@router.get("", response_model=CartOut)
def read_cart(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    """The basket, priced from the catalogue as it stands.

    Read at a handful of points - the cart page, and the pull half of the
    sign-in reconcile - rather than on every render. The browser is
    local-first; this is not the render path. This is the one response that
    carries `deleted_lines`, because this is the one the client reconciles
    against.
    """
    return _cart_out(db, CartOwner.user(current_user["sub"]), with_tombstones=True)


@router.put("", response_model=CartOut)
def sync_cart(
    payload: CartMergeRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    """Reconcile a device's local basket with the server's, one sync point at a
    time (idle, tab hide, sign-in, sign-out).

    This is the write path the browser actually uses. A whole basket, not one
    line, because the browser edits locally and only talks to the server here -
    see Biofarm_KnowledgeBase/documentation/designs/2026-09-08-local-first-cart-sync.md.
    Merged per line rather than replaced outright: see merge_cart for why a
    whole-basket replace would let one stale line clobber every other line's
    genuine changes.

    The response is the merged basket, but the client does not read it - a push
    is fire-and-forget, and local state is reconciled only at a pull point
    (GET /cart). So it omits `deleted_lines`; GET carries those.
    """
    owner = CartOwner.user(current_user["sub"])
    merge_cart(
        db,
        owner,
        [
            CartLineMerge(
                variant_id=item.variant_id,
                quantity=item.quantity,
                client_updated_at=item.client_updated_at,
                deleted=item.deleted,
            )
            for item in payload.items
        ],
    )
    return _cart_out(db, owner)


@router.put("/items/{variant_id}", response_model=CartOut)
def put_line(
    variant_id: uuid.UUID,
    payload: CartLineIn,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    """Set one line to a quantity, right now, unconditionally.

    Not the sync path - see PUT /cart - but kept for direct, single-line use: a
    quantity set this way always wins whatever it touches.
    """
    owner = CartOwner.user(current_user["sub"])
    try:
        set_line(db, owner, variant_id, payload.quantity)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return _cart_out(db, owner)


@router.delete("/items/{variant_id}", response_model=CartOut)
def delete_line(
    variant_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    owner = CartOwner.user(current_user["sub"])
    remove_line(db, owner, variant_id)
    return _cart_out(db, owner)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def empty_cart(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    clear_cart(db, CartOwner.user(current_user["sub"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
