import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_admin
from app.models.order import OrderStatus
from app.models.product_variant import ProductVariant
from app.schemas.order import AdminOrderItemOut, AdminOrderOut, AdminOrderPage, UpdateOrderStatusRequest, UpdateTrackingRequest
from app.services.order_service import (
    cancel_order,
    confirm_order_admin,
    deliver_order,
    get_order_by_id,
    DEFAULT_ORDER_PAGE_SIZE,
    HOLD_FILTERS,
    MAX_ORDER_PAGE_SIZE,
    authorization_days_remaining,
    list_all_orders,
    load_order_for_update,
    ship_order,
    update_tracking_number,
)
from app.services.cognito_service import get_account
from app.services.stripe_service import (
    release_funds,
    capture_payment_intent,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/orders", tags=["admin-orders"])


def _enrich_items_with_stock(order, db: Session):
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
        authorization_days_remaining=authorization_days_remaining(order),
        captured_at=order.captured_at,
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
        # Omitted until now, and the schema default of 0 made that silent: every
        # admin response reported no shipping whatever the order held. It was a
        # display bug while nothing added the parts up; the confirm dialog now
        # quotes total_amount + shipping + tax as the sum about to be charged,
        # so a missing fee means telling an admin they are charging less than
        # Stripe actually captures - and the customer's own order page, built
        # from the ORM object, shows the real figure.
        shipping_amount=order.shipping_amount,
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


# Only decides whether a Cognito lookup is worth attempting, so it is loose on
# purpose - a false positive costs one lookup, which is then cached as a miss.
#
# It does not, and cannot, promise that a half-typed address never resolves:
# "alice@lab.co" is both a real address and a prefix of "alice@lab.com". What
# actually bounds the calls is the console's 300ms debounce and the negative
# cache; the two-character minimum below just removes the noisiest case, since a
# pause mid-TLD is where typing most often stops.
_LOOKS_LIKE_EMAIL = re.compile(r"\A[^@\s]+@[^@\s]+\.[^@\s]{2,}\Z")


def _sub_for_search(search: Optional[str]) -> Optional[str]:
    """The account behind a searched-for email address, if there is one.

    The point of the whole thing: `customer_email` holds where the customer
    asked order mail to go, and support hears about it precisely when that was
    wrong. Searching the address they actually have would otherwise match
    nothing, and the admin would have to know to look up a sub by hand.

    Never raises. A search must not fail because Cognito is unreachable - the
    text matches are still worth returning, and an admin who gets an error
    instead of a partial result has no way to tell the difference between
    "no such customer" and "AWS is down".
    """
    if not search or not _LOOKS_LIKE_EMAIL.match(search.strip()):
        return None

    try:
        account = get_account(search.strip())
    except (ValueError, BotoCoreError, ClientError):
        logger.warning("could not resolve %r to an account; searching text only", search)
        return None

    return account["sub"] if account else None


@router.get("", response_model=AdminOrderPage)
def list_orders(
    # Named status_filter because a parameter called `status` shadows the
    # fastapi `status` module imported above - which is why this function alone
    # had to hardcode 400 where every sibling uses the constant. The alias keeps
    # the query string unchanged.
    status_filter: Optional[str] = Query(default=None, alias="status"),
    search: Optional[str] = Query(
        default=None,
        alias="q",
        description=(
            "Matches order number, customer email, shipping name, user id or "
            "order id. An email is also resolved against Cognito, so a customer's "
            "own address finds their orders even when they sent the confirmation "
            "somewhere else."
        ),
    ),
    hold: Optional[str] = Query(
        default=None,
        description=(
            "Narrow to orders by the state of their card authorisation: "
            "'expiring' (running out, still chargeable) or 'expired' (past the "
            "window, so shipping fails at capture). These are what the dashboard "
            "card-hold tiles link to."
        ),
    ),
    limit: int = Query(default=DEFAULT_ORDER_PAGE_SIZE, ge=1, le=MAX_ORDER_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    # `?hold=` with no value is absent, not invalid - the same reading `status`
    # gets. A cleared filter often serialises as an empty parameter, and 400ing
    # on it turns a link that merely says nothing into an error page.
    hold = hold or None
    if hold is not None and hold not in HOLD_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid hold filter: {hold}",
        )

    order_status = None
    if status_filter:
        try:
            order_status = OrderStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status: {status_filter}",
            )
    orders, total = list_all_orders(
        db,
        order_status,
        search=search,
        also_user_id=_sub_for_search(search),
        hold=hold,
        limit=limit,
        offset=offset,
    )
    return AdminOrderPage(
        items=[_build_admin_order_out(o, db) for o in orders],
        total=total,
        limit=limit,
        offset=offset,
    )


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
    # Locked for the whole transition. Everything below reads the order and then
    # acts on what it read - captured or not, shippable or not - and two admins
    # doing that at once both acted on the same stale answer.
    order = load_order_for_update(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    try:
        if payload.status == "confirmed":
            # Captured here, not at ship. Checkout only authorises, and a card
            # hold lapses after about a week - so capturing at ship put the
            # deadline on the slowest step. Packing cold-chain goods and waiting
            # for a courier can outrun the hold, and the failure surfaced with
            # the box already packed and the stock already deducted.
            #
            # Confirming is a click, comfortably inside the window, and it is
            # already the moment the admin commits: stock has been validated and
            # set aside. Taking the money there means shipping is no longer on a
            # clock.
            #
            # The cost is that walking away is no longer free. Cancelling after
            # this refunds rather than voids, and Stripe keeps its fee - see
            # cancel_order below, where the boundary moved to match.
            #
            # Before the status change, deliberately: a confirmed order whose
            # capture failed would otherwise sit with its stock deducted and no
            # money behind it.
            if order.status != OrderStatus.awaiting_fulfillment:
                raise ValueError(f"Cannot confirm order in status {order.status.value}")

            # Skipped if it already happened. Without the record, a retry after a
            # failed commit captured a second time, which Stripe rejects - a
            # permanent 502 on an order that could never be confirmed.
            if order.captured_at is None:
                try:
                    # Asked, not assumed. Committing captured_at separately made
                    # it durable but ended the transaction, releasing the row
                    # lock between the capture and the status change - and a
                    # cancel landing in that gap refunded and restocked an order
                    # that then became confirmed. Charged, refunded, restocked
                    # and confirmed, all at once.
                    #
                    # So this stays one transaction, and the rollback case is
                    # handled by asking Stripe whether the money already moved
                    # rather than by trusting a record that may not have
                    # survived. Stripe is authoritative for that, as it is for
                    # every other money question here.
                    capture_payment_intent(order.stripe_payment_intent_id)
                except Exception as e:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Stripe capture failed: {e}")

                # Set, not committed. confirm_order_admin's commit persists this
                # with the status change, so the lock taken above is held across
                # both and the interleaving cannot happen.
                order.captured_at = datetime.now(tz=timezone.utc)

            order = confirm_order_admin(db, order_id)
        elif payload.status == "shipped":
            if order.status != OrderStatus.confirmed:
                raise ValueError(f"Cannot ship order in status {order.status.value}")

            # Normally a no-op: the money was taken at confirm. It matters for
            # orders confirmed *before* the capture moved, which carry no
            # captured_at - without this they would ship and the authorisation
            # would lapse uncaptured, and the merchant would never be paid.
            # No backfill needed; the fact is checked rather than the status.
            if order.captured_at is None:
                try:
                    # Same shape as confirm, deliberately: the two money-moving
                    # branches must not disagree about how a lost record
                    # recovers.
                    capture_payment_intent(order.stripe_payment_intent_id)
                except Exception as e:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Stripe capture failed: {e}")
                # Set, not committed: ship_order's commit persists it, keeping the
                # transition one transaction and the lock unbroken.
                order.captured_at = datetime.now(tz=timezone.utc)

            order = ship_order(db, order_id, tracking_number=payload.tracking_number)
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
    # Locked for the same reason as the status transition: this reads whether
    # the money moved and then voids or refunds on the answer. A confirm running
    # concurrently would otherwise charge the card while this cancelled the
    # order and took the void branch - charged, cancelled, and never refunded.
    order = load_order_for_update(db, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    if order.status == OrderStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order is already cancelled"
        )

    try:
        # Asks Stripe, like confirm and ship do. This was the third money branch
        # and the one left behind.
        #
        # Void or refund, decided by where the money actually got to.
        #
        # Neither local answer is trustworthy on its own. `captured_at` is NULL
        # on every row predating the column - including orders shipped under the
        # old rule, where capture happened at ship and the money really did move
        # - so trusting it alone voids a captured intent, which Stripe rejects,
        # and the customer is never refunded. Status stopped implying anything
        # about capture the moment that moved to confirm.
        #
        # Asking Stripe up front was the first fix and cost too much: a live
        # retrieve on every cancellation, inside this `try` whose handler is a
        # 502, while the row lock is held. A blip on a call that usually only
        # confirms what we already knew wedged the cancellation.
        #
        # release_funds discovers it instead - one round trip normally, and the
        # rare captured-but-unrecorded case corrects itself.
        release_funds(
            order.stripe_payment_intent_id,
            known_captured=order.captured_at is not None,
        )
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Stripe operation failed: {e}")

    try:
        order = cancel_order(db, order_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return _build_admin_order_out(order, db)
