import hashlib
import json
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.dependencies.auth import require_user, verify_id_token
from app.models.product_variant import ProductVariant
from app.models.order import OrderStatus
from app.schemas.order import CreatePaymentIntentRequest, OrderOut, PaymentIntentResponse
from app.services.order_service import (
    cancel_order_by_customer,
    create_order,
    get_order_by_id,
    get_order_by_payment_intent,
    get_orders_for_user,
    save_checkout_session,
)
from app.services.shipping_service import calculate_shipping
from app.services.stripe_service import calculate_tax, cancel_payment_intent, create_payment_intent

router = APIRouter(prefix="/orders", tags=["orders"])


def _get_customer_email(current_user: dict, id_token: str = "") -> str:
    """The customer's email, or "" if it cannot be established.

    An access token carries no email claim, so the frontend forwards the id token
    in X-Id-Token. That token is now fully verified - signature, issuer, audience
    and token_use - rather than merely decoded.

    It used to be read with the signature check disabled, on the grounds that the
    value was only a hint and never an authorization decision. That was true of
    how it is *used* and beside the point: the email is written onto the order
    and shown in the admin console, so an unverified claim let any signed-in
    customer put an arbitrary address - including someone else's - into the
    record fulfilment works from.

    Empty is an acceptable answer. A checkout must not fail over a missing email.
    """
    email = current_user.get("email", "")
    if email:
        return email
    if not id_token:
        return ""

    claims = verify_id_token(id_token)
    if claims is None:
        return ""

    # The id token must belong to the same person as the access token that
    # authenticated this request, or a valid token borrowed from elsewhere would
    # do just as well.
    if claims.get("sub") != current_user.get("sub"):
        return ""

    return claims.get("email", "")


def _idempotency_key(user_sub: str, payload: CreatePaymentIntentRequest, total_cents: int) -> str:
    """A key that is stable for one checkout attempt and different for the next.

    Stripe deduplicates creates that carry the same key, which is what stops a
    double-clicked Pay button or a network-level retry from opening a second
    PaymentIntent - a second authorization hold on the customer's card, against
    the same basket, that nothing will ever void.

    Derived from the buyer, the cart, the destination and the amount, so
    genuinely re-submitting a changed basket gets its own intent while a repeat
    of the identical request does not. Stripe expires these after 24 hours,
    which comfortably outlives a checkout.
    """
    material = json.dumps(
        {
            "sub": user_sub,
            "cart": sorted((str(i.variant_id), i.quantity) for i in payload.cart),
            "shipping": payload.shipping.model_dump(mode="json"),
            "total_cents": total_cents,
        },
        sort_keys=True,
    )
    return f"checkout_{hashlib.sha256(material.encode()).hexdigest()[:48]}"


@router.post(
    "/payment-intent",
    response_model=PaymentIntentResponse,
    status_code=status.HTTP_201_CREATED,
)
def initiate_checkout(
    request: Request,
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

        for cart_item in payload.cart:
            variant = variants.get(cart_item.variant_id)
            if variant is None:
                raise ValueError(f"Variant {cart_item.variant_id} not found")
            if variant.stock < cart_item.quantity:
                raise ValueError(
                    f"Insufficient stock for variant {cart_item.variant_id}: "
                    f"requested {cart_item.quantity}, available {variant.stock}"
                )

        subtotal_cents = int(sum(
            variants[item.variant_id].price * item.quantity for item in payload.cart
        ) * 100)

        line_items_for_tax = [
            {
                "amount": int(variants[item.variant_id].price * item.quantity * 100),
                "reference": str(item.variant_id),
                "tax_code": "txcd_99999999",
            }
            for item in payload.cart
        ]
        shipping_address = {
            "line1": payload.shipping.address1,
            "city": payload.shipping.city,
            "state": payload.shipping.state,
            "postal_code": payload.shipping.zip,
            "country": "US",
        }
        # Quoted once, here, and carried everywhere after. Recomputing it later
        # risks charging a rate the customer was never shown.
        shipping_cents = calculate_shipping(payload.cart)

        try:
            tax_result = calculate_tax(line_items_for_tax, shipping_address, shipping_cents)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Tax calculation failed: {e}. Ensure your Stripe Tax origin address is configured in the Stripe Dashboard.",
            )

        pi = create_payment_intent(
            tax_result.total_cents,
            order_id=None,
            idempotency_key=_idempotency_key(current_user["sub"], payload, tax_result.total_cents),
        )
        id_token = request.headers.get("X-Id-Token", "")
        # What the customer asked for, falling back to the address on their
        # account. Order mail is a notification, not a credential - nothing is
        # authorised by it, and every order is scoped by user_id - so taking the
        # customer's word for where to send it is safe. A lab ordering against a
        # shared purchasing address cannot say so otherwise.
        #
        # The verified id-token address still earns its keep as that fallback.
        customer_email = payload.contact_email or _get_customer_email(current_user, id_token)

        if get_settings().stripe_bypass:
            # No webhook in bypass mode — create the order immediately with mock card info
            order = create_order(
                db=db,
                user_id=current_user["sub"],
                cart=payload.cart,
                shipping=payload.shipping,
                stripe_pi_id=pi.id,
                tax_amount=Decimal(tax_result.tax_amount_cents) / 100,
                shipping_amount=Decimal(shipping_cents) / 100,
                customer_email=customer_email,
                card_brand="visa",
                card_last4="4242",
            )
            order.status = OrderStatus.awaiting_fulfillment
            db.commit()
            return PaymentIntentResponse(
                client_secret=pi.client_secret,
                order_id=order.id,
                subtotal_cents=subtotal_cents,
                tax_amount_cents=tax_result.tax_amount_cents,
                shipping_amount_cents=shipping_cents,
            )

        save_checkout_session(
            db,
            stripe_pi_id=pi.id,
            user_id=current_user["sub"],
            cart=payload.cart,
            shipping=payload.shipping,
            tax_amount_cents=tax_result.tax_amount_cents,
            shipping_amount_cents=shipping_cents,
            customer_email=customer_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return PaymentIntentResponse(
        client_secret=pi.client_secret,
        subtotal_cents=subtotal_cents,
        tax_amount_cents=tax_result.tax_amount_cents,
        shipping_amount_cents=shipping_cents,
    )


@router.get("", response_model=list[OrderOut])
def list_my_orders(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    return get_orders_for_user(db, current_user["sub"])


@router.get("/by-payment-intent/{pi_id}", response_model=OrderOut)
def get_my_order_by_payment_intent(
    pi_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    order = get_order_by_payment_intent(db, pi_id)
    if order is None or order.user_id != current_user["sub"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


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


@router.post("/{order_id}/cancel", response_model=OrderOut)
def cancel_my_order(
    order_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    order = get_order_by_id(db, order_id)
    if order is None or order.user_id != current_user["sub"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.status not in (OrderStatus.pending, OrderStatus.awaiting_fulfillment):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel order in status '{order.status.value}'"
        )

    try:
        cancel_payment_intent(order.stripe_payment_intent_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Stripe operation failed: {e}")

    # Through the service rather than setting the status here. Cancelling now
    # means returning the order's stock as well as marking it, and this endpoint
    # used to do only the marking - so a customer cancelling their own order
    # destroyed the inventory it was holding, while the admin doing the same
    # thing returned it.
    try:
        return cancel_order_by_customer(db, order_id, current_user["sub"])
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
