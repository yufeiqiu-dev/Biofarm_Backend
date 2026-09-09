import json
import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.checkout_session import CheckoutSession
from app.services.cart_service import CartOwner, clear_bought_lines
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product_variant import ProductVariant
from app.schemas.order import CartItemIn, ShippingIn
from app.services.order_numbers import generate_order_number
from app.services import email_service


logger = logging.getLogger(__name__)

# How many times to re-roll an order number when the generated one is already
# taken. Against 32^8 possibilities a single collision is already unlikely; five
# in a row is not worth planning for beyond failing loudly.
_ORDER_NUMBER_ATTEMPTS = 5


# How long Stripe holds a manual-capture authorisation before it lapses.
#
# Checkout only authorises; the money is captured when the admin confirms. So an
# order sitting *unconfirmed* past this window has an authorisation that has
# quietly died, and confirming it fails.
#
# This used to be the deadline on shipping, which was the wrong step to hang it
# on: packing cold-chain goods and waiting for a courier can outrun a week,
# while confirming is a click. Moving the capture to confirm shortened the race
# to something an admin can reasonably win; the clock still matters, because an
# order can still be left unconfirmed over a holiday.
AUTHORIZATION_HOLD_DAYS = 7

# When to start saying so. Two days is enough to act on without the warning
# being permanently on screen.
AUTHORIZATION_WARN_DAYS = 2

# Statuses where an order is still live and could still be captured. Whether it
# *has* been is captured_at's job, not this tuple's.
#
# This listed only pending and awaiting_fulfillment, on the reasoning that
# confirming captures - true for orders confirmed under the new rule, and wrong
# for the ones this whole design exists to handle. An order confirmed before the
# capture moved has status=confirmed and captured_at=NULL: a live hold, still
# ticking, which the ship path will try to capture. Keyed on status alone it got
# no warning anywhere, which is exactly the failure the clock was added to
# surface.
LIVE_UNSHIPPED_STATUSES = (
    OrderStatus.pending,
    OrderStatus.awaiting_fulfillment,
    OrderStatus.confirmed,
)


# The two card-hold cohorts, as SQL.
#
# Defined once because the dashboard counts them and the order list has to show
# the same rows. When each spelled out its own window, the tile counted three
# orders and the link it offered led to a list that could not show them - an
# alarm with no route to the thing it was alarming about.
HOLD_EXPIRING = "expiring"
HOLD_EXPIRED = "expired"
HOLD_FILTERS = (HOLD_EXPIRING, HOLD_EXPIRED)


def hold_criteria(hold: str, now: datetime | None = None) -> list:
    """Where-clauses selecting orders by the state of their card authorisation.

    `expiring` is still chargeable but running out; `expired` has passed the
    window, so shipping it now fails at capture and the honest action is to
    cancel and return the stock. The advice differs, which is why they are two
    cohorts rather than one "old orders" bucket.

    Both are bounded to live, unshipped, uncaptured orders: once the money has
    moved there is no hold left to lapse.
    """
    if hold not in HOLD_FILTERS:
        raise ValueError(f"Unknown hold filter: {hold}")

    now = now or datetime.now(tz=timezone.utc)
    warn_from = now - timedelta(days=AUTHORIZATION_HOLD_DAYS - AUTHORIZATION_WARN_DAYS)
    dead_from = now - timedelta(days=AUTHORIZATION_HOLD_DAYS)

    criteria = [
        Order.status.in_(LIVE_UNSHIPPED_STATUSES),
        Order.captured_at.is_(None),
    ]
    if hold == HOLD_EXPIRING:
        criteria += [Order.created_at <= warn_from, Order.created_at > dead_from]
    else:
        # Deliberately open-ended at the far end. An expired hold was bounded to
        # a fortnight so one abandoned order could not pin a red tile forever -
        # but the order goes on holding its stock indefinitely, and dropping it
        # from the dashboard made that leak invisible rather than fixing it. The
        # tile now links to exactly these rows, so an admin clears it by
        # cancelling them, which is the action that returns the stock.
        criteria.append(Order.created_at <= dead_from)
    return criteria


# What a customer may cancel themselves.
#
# Named because two paths' correctness depends on it, and it was written out
# twice. Capture happens at confirm - and happened at ship before that - so the
# money has provably not moved in any status here. That is what lets the cancel
# endpoint trust captured_at alone instead of asking Stripe on every
# cancellation. Adding `confirmed` would silently start voiding intents that
# hold real money.
#
# Lives in the service rather than the endpoint because the service holds the
# other copy, and a service importing from an endpoint inverts the layering.
CUSTOMER_CANCELLABLE_STATUSES = (OrderStatus.pending, OrderStatus.awaiting_fulfillment)


def authorization_days_remaining(order: Order) -> float | None:
    """Days before this order's authorisation lapses, or None if it cannot.

    Negative once it already has - which is worth showing rather than clamping,
    because "expired 3 days ago" and "expires today" call for different actions.

    Measured from created_at. The authorisation is placed when the customer
    pays, which is when the webhook creates the order; the two are minutes
    apart, and the window is a week.
    """
    # Both conditions. The status says the order is still live; captured_at says
    # whether there is still a hold to lose.
    if order.status not in LIVE_UNSHIPPED_STATUSES:
        return None
    if order.captured_at is not None:
        return None

    created = order.created_at
    if created is None:
        return None
    if created.tzinfo is None:
        # SQLite hands back naive datetimes; Postgres tz-aware ones.
        created = created.replace(tzinfo=timezone.utc)

    expires = created + timedelta(days=AUTHORIZATION_HOLD_DAYS)
    return round((expires - datetime.now(tz=timezone.utc)).total_seconds() / 86400, 2)


def _load_order(db: Session, order_id: uuid.UUID) -> Order | None:
    return db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
    )


def load_order_for_update(db: Session, order_id: uuid.UUID) -> Order | None:
    """Load an order with its row locked for the rest of the transaction.

    Every money decision on an order is a read followed by a write - is it
    captured, may it be cancelled, has it shipped - and without a lock two
    admins acting at once both read the same "not captured yet" and both act on
    it. Measured against real Postgres: two simultaneous confirms produced two
    captures, and Stripe rejects the second, so the admin sees a failure on an
    order that was in fact charged.

    populate_existing for the same reason it is needed when taking stock: get()
    will take the lock and leave already-loaded attributes stale, so the read
    that follows would be the value from before the wait.

    The Stripe call then happens while this lock is held, which is a network
    round trip inside a transaction. That is a real cost and it is the right
    trade here: the rows are per-order, one admin is the expected load, and the
    alternative is charging a customer twice.
    """
    return db.get(Order, order_id, with_for_update=True, populate_existing=True)


# Newest first, with id as a tiebreaker.
#
# The tiebreak is the load-bearing half. created_at is not unique - Postgres
# now() is transaction-start, so anything written together ties, and SQLite's
# CURRENT_TIMESTAMP only has second granularity. With ties, LIMIT/OFFSET has no
# defined order between one page request and the next: OFFSET does not remember
# what the previous page returned, it re-runs the query and skips N. A row can
# then appear on two pages while another is never shown at all.
#
# Named once because both listings must sort identically; they diverged silently
# when each spelled it out.
ORDER_LISTING_SORT = (Order.created_at.desc(), Order.id.desc())


def _load_order_by_pi(db: Session, pi_id: str) -> Order | None:
    return db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.stripe_payment_intent_id == pi_id)
    )


def _aggregate_demand(
    entries: "Iterable[tuple[uuid.UUID | None, int, str]]",
) -> dict[uuid.UUID, tuple[int, str]]:
    """Collapse (variant_id, quantity, product_name) triples per variant.

    A cart, and an order, may legitimately hold two lines for the same variant.
    Checking them one at a time passes each on its own and misses the combined
    total, which is how an order for 3 + 3 of a variant holding 5 used to get
    through. The name is kept only so the error can say what ran out.

    Entries with no variant_id are skipped: OrderItem.variant_id is
    ON DELETE SET NULL, so an order outliving its product has nothing to return
    stock to.
    """
    demand: dict[uuid.UUID, tuple[int, str]] = {}
    for variant_id, quantity, name in entries:
        if variant_id is None:
            continue
        running, first_name = demand.get(variant_id, (0, name))
        demand[variant_id] = (running + quantity, first_name)
    return demand


def _lock_in_order(db: Session, demand: dict[uuid.UUID, tuple[int, str]]):
    """Yield (variant, quantity, name) with every row locked for the transaction.

    Sorted by id so concurrent transactions take the same rows in the same
    order. Two of them locking the same two variants in opposite orders deadlock,
    and Postgres resolves that by killing one.

    populate_existing is not optional here, and its absence does not look like a
    bug. create_order loads the variants before it reaches this, so they are
    already in the identity map - and Session.get() will happily take the lock
    while leaving those stale attributes untouched. The row is then correctly
    locked and the value read from it is the one fetched before waiting, which is
    exactly the lost update the lock is meant to prevent: measured against real
    Postgres, two buyers both took the last unit and both got an order. SQLite
    cannot reproduce it, so no amount of running the suite would have shown it.
    """
    for variant_id, (quantity, name) in sorted(demand.items(), key=lambda e: str(e[0])):
        variant = db.get(
            ProductVariant, variant_id, with_for_update=True, populate_existing=True
        )
        if variant is not None:
            yield variant, quantity, name


def _take_stock(db: Session, demand: dict[uuid.UUID, tuple[int, str]]) -> None:
    """Deduct, or raise having changed nothing.

    Validated in full before anything is written, so a cart whose third variant
    is short does not leave the first two decremented for the caller to unpick.
    The lock makes the read and the write one atomic step - without it two
    transactions read the same count, both pass, and both deduct.
    """
    locked: list[tuple[ProductVariant, int]] = []
    for variant, quantity, name in _lock_in_order(db, demand):
        if variant.stock < quantity:
            raise ValueError(
                f"Insufficient stock for {name}: need {quantity}, only {variant.stock} available"
            )
        locked.append((variant, quantity))

    for variant, quantity in locked:
        variant.stock -= quantity


def _return_stock(db: Session, demand: dict[uuid.UUID, tuple[int, str]]) -> None:
    """Give stock back. Locked for the same reason: a restore that reads a stale
    count writes one back."""
    for variant, quantity, _name in _lock_in_order(db, demand):
        variant.stock += quantity


def create_order(
    db: Session,
    user_id: str,
    cart: list[CartItemIn],
    shipping: ShippingIn,
    stripe_pi_id: str,
    tax_amount: Decimal = Decimal("0"),
    shipping_amount: Decimal = Decimal("0"),
    customer_email: str = "",
    card_brand: str = "",
    card_last4: str = "",
) -> Order:
    variant_ids = [item.variant_id for item in cart]
    variants = {
        v.id: v
        for v in db.scalars(
            select(ProductVariant)
            .options(selectinload(ProductVariant.product))
            .where(ProductVariant.id.in_(variant_ids))
        ).all()
    }

    # Plain values rather than ORM objects: a retry below needs to build fresh
    # OrderItems, and an instance from a rolled-back attempt cannot be reused.
    item_values: list[dict] = []
    total = Decimal("0")
    for cart_item in cart:
        variant = variants.get(cart_item.variant_id)
        if variant is None:
            raise ValueError(f"Variant {cart_item.variant_id} not found")
        total += variant.price * cart_item.quantity
        item_values.append(
            {
                "variant_id": variant.id,
                "product_name": variant.product.name if variant.product else "Unknown",
                "variant_label": f"{variant.size_value} {variant.size_unit}",
                "unit_price": variant.price,
                "quantity": cart_item.quantity,
            }
        )

    def build(order_number: str) -> Order:
        return Order(
            order_number=order_number,
            user_id=user_id,
            customer_email=customer_email,
            card_brand=card_brand,
            card_last4=card_last4,
            status=OrderStatus.pending,
            stripe_payment_intent_id=stripe_pi_id,
            shipping_name=shipping.name,
            shipping_phone=shipping.phone,
            shipping_address1=shipping.address1,
            shipping_address2=shipping.address2,
            shipping_city=shipping.city,
            shipping_state=shipping.state,
            shipping_zip=shipping.zip,
            notes=shipping.notes,
            total_amount=total,
            tax_amount=tax_amount,
            shipping_amount=shipping_amount,
            items=[OrderItem(**values) for values in item_values],
        )

    # The number is random rather than derived from what other rows hold, so the
    # old read-then-write race is gone entirely. The retry stays for the far
    # rarer case of two generated values colliding, and because an unhandled
    # IntegrityError here is a 500 on a request whose card is already authorized
    # - the customer charged, with no order.
    # Aggregated once: the demand does not change between retries, and
    # item_values has already resolved every name and rejected a missing variant.
    demand = _aggregate_demand(
        (values["variant_id"], values["quantity"], values["product_name"])
        for values in item_values
    )

    for attempt in range(_ORDER_NUMBER_ATTEMPTS):
        order = build(generate_order_number())
        db.add(order)
        try:
            # Inside the loop, not before it: the rollback below undoes the
            # deduction along with the order, so each attempt has to take it
            # again. Stock and order commit together or not at all.
            _take_stock(db, demand)
            db.commit()
        except ValueError:
            # Out of stock. No retry will change that, and the order must not
            # survive the attempt that failed.
            db.rollback()
            raise
        except IntegrityError:
            db.rollback()
            # orders has a second unique column, stripe_payment_intent_id, and
            # retrying a conflict on that one would just fail five times and
            # hide the real cause. If an order for this intent now exists, the
            # collision was not the number.
            if _load_order_by_pi(db, stripe_pi_id) is not None:
                raise
            if attempt == _ORDER_NUMBER_ATTEMPTS - 1:
                raise
            continue
        db.refresh(order)
        return order

    raise RuntimeError("unreachable")  # pragma: no cover


def save_checkout_session(
    db: Session,
    stripe_pi_id: str,
    user_id: str,
    cart: list[CartItemIn],
    shipping: ShippingIn,
    # Keyword-only from here. Adding shipping_amount_cents in the middle of this
    # list silently rebound a positional caller's email string to a cents
    # argument - no type error, just an order stored with an empty address and a
    # nonsense shipping charge. A bare * makes that impossible for the next
    # field anyone adds.
    *,
    tax_amount_cents: int = 0,
    shipping_amount_cents: int = 0,
    customer_email: str = "",
) -> CheckoutSession:
    """Record the cart against a PaymentIntent, replacing any existing record.

    Idempotent on purpose, and it has to be. Checkout sends Stripe an idempotency
    key derived from the buyer and the cart, so a resubmitted checkout - a
    double-clicked Pay button, a lost response, a browser retry - gets *the same*
    PaymentIntent back. That is the point of the key: it stops a second
    authorization hold on the customer's card.

    But the same id then arrives here, where stripe_pi_id is unique. Inserting
    blindly raised an IntegrityError and returned a 500, so the very retry the
    idempotency key made safe at Stripe was fatal one layer down. The two halves
    have to agree.

    Updating rather than skipping matters too: the shipping address or the tax
    may legitimately have changed between attempts, and the webhook builds the
    order from whatever is stored here.
    """
    cart_json = json.dumps(
        [{"variant_id": str(item.variant_id), "quantity": item.quantity} for item in cart]
    )

    session = db.scalar(
        select(CheckoutSession).where(CheckoutSession.stripe_pi_id == stripe_pi_id)
    )
    if session is None:
        session = CheckoutSession(stripe_pi_id=stripe_pi_id)
        db.add(session)

    session.user_id = user_id
    session.customer_email = customer_email
    session.cart_json = cart_json
    session.shipping_json = shipping.model_dump_json()
    session.tax_amount_cents = tax_amount_cents
    session.shipping_amount_cents = shipping_amount_cents

    db.commit()
    db.refresh(session)
    return session


def create_order_from_checkout_session(
    db: Session,
    stripe_pi_id: str,
    card_brand: str = "",
    card_last4: str = "",
) -> Order | None:
    session = db.scalar(
        select(CheckoutSession).where(CheckoutSession.stripe_pi_id == stripe_pi_id)
    )
    if session is None:
        return None

    cart = [CartItemIn(variant_id=item["variant_id"], quantity=item["quantity"]) for item in json.loads(session.cart_json)]
    shipping = ShippingIn.model_validate_json(session.shipping_json)
    tax_amount = Decimal(session.tax_amount_cents) / 100
    shipping_amount = Decimal(session.shipping_amount_cents) / 100

    order = create_order(db, user_id=session.user_id, cart=cart, shipping=shipping, stripe_pi_id=stripe_pi_id, tax_amount=tax_amount, shipping_amount=shipping_amount, customer_email=session.customer_email, card_brand=card_brand, card_last4=card_last4)
    order.status = OrderStatus.awaiting_fulfillment

    # Read before the delete below. Touching an attribute on a session row that
    # a flush has already removed tries to refresh a row that is no longer
    # there, which raised ObjectDeletedError from inside the webhook - and
    # whether a flush has happened by then depends on what the cart work does,
    # which is not a thing to rely on.
    buyer = CartOwner.user(session.user_id)

    db.delete(session)
    db.commit()

    # After the commit, and unable to fail it.
    #
    # The order is what matters and it is now durable. Sharing the commit meant
    # a lock timeout on a cart row - the customer's other device editing the
    # basket at that moment - failed the whole webhook, and Stripe retries a
    # webhook whose CheckoutSession is still there: a second order for one
    # payment. A basket that did not empty is a far smaller problem, and the
    # customer can empty it. Same reasoning as the confirmation email below.
    try:
        clear_bought_lines(db, buyer, [(item.variant_id, item.quantity) for item in cart])
        db.commit()
    except Exception:  # noqa: BLE001 - the order matters, the basket does not
        db.rollback()
        logger.exception("could not empty the basket after order %s", order.order_number)

    db.refresh(order)

    # After the commit, deliberately. The order is the thing that matters and it
    # is already durable; email_service swallows its own failures so a bad send
    # cannot turn into a non-200 back to Stripe and a retried, duplicated order.
    email_service.send_order_confirmation(order)
    return order


def delete_checkout_session(db: Session, stripe_pi_id: str) -> None:
    session = db.scalar(
        select(CheckoutSession).where(CheckoutSession.stripe_pi_id == stripe_pi_id)
    )
    if session:
        db.delete(session)
        db.commit()


def cleanup_stale_checkout_sessions(db: Session, max_age_days: int = 8) -> int:
    """Delete sessions older than max_age_days.

    A session that never received either a payment or a payment_intent.canceled
    webhook - the backend was down, or the customer simply closed the tab - is
    otherwise there forever. Run from app.jobs.cleanup on a schedule; this used
    to run in the startup hook, which meant a table scan and delete at the exact
    moment the service was trying to become healthy.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    stale = db.scalars(
        select(CheckoutSession).where(CheckoutSession.created_at < cutoff)
    ).all()
    count = len(stale)
    for session in stale:
        db.delete(session)
    if count:
        db.commit()
    return count


def get_orders_for_user(db: Session, user_id: str) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.user_id == user_id)
            .order_by(*ORDER_LISTING_SORT)
        ).all()
    )


def get_order_by_id(db: Session, order_id: uuid.UUID) -> Order | None:
    return _load_order(db, order_id)


def get_order_by_payment_intent(db: Session, pi_id: str) -> Order | None:
    return _load_order_by_pi(db, pi_id)


def confirm_order_admin(db: Session, order_id: uuid.UUID) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status != OrderStatus.awaiting_fulfillment:
        raise ValueError(f"Cannot confirm order in status {order.status.value}")

    # Deliberately does not touch stock. It was taken when the order was
    # created, so by the time an admin gets here the inventory is already set
    # aside and confirming cannot fail on it. It used to deduct, which meant the
    # moment an admin happened to click was what decided whether inventory
    # moved - and a second customer who had already paid for the last unit only
    # found out when this raised.
    order.status = OrderStatus.confirmed

    db.commit()
    db.refresh(order)
    return order


def ship_order(db: Session, order_id: uuid.UUID, tracking_number: str | None = None) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status != OrderStatus.confirmed:
        raise ValueError(f"Cannot ship order in status {order.status.value}")
    order.status = OrderStatus.shipped
    if tracking_number:
        order.tracking_number = tracking_number
    db.commit()
    db.refresh(order)

    # The payment was captured before this status change, so the shipment has
    # really happened; a failed email must not unwind it.
    email_service.send_order_shipped(order)
    return order


def update_tracking_number(db: Session, order_id: uuid.UUID, tracking_number: str) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status not in (OrderStatus.shipped, OrderStatus.delivered):
        raise ValueError("Tracking number can only be set on shipped or delivered orders")
    order.tracking_number = tracking_number
    db.commit()
    db.refresh(order)
    return order


def deliver_order(db: Session, order_id: uuid.UUID) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status != OrderStatus.shipped:
        raise ValueError(f"Cannot deliver order in status {order.status.value}")
    order.status = OrderStatus.delivered
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: uuid.UUID) -> Order:
    order = _load_order(db, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} not found")
    if order.status == OrderStatus.cancelled:
        raise ValueError(f"Order is already cancelled")

    # Unconditional, because every order that exists holds its stock from the
    # moment it was created. This used to be conditional on having passed
    # confirm, which was the only point stock moved; now the guard above -
    # refusing an already-cancelled order - is the only thing standing between
    # this and a double restore.
    _return_stock(
        db,
        _aggregate_demand(
            (item.variant_id, item.quantity, item.product_name) for item in order.items
        ),
    )

    order.status = OrderStatus.cancelled
    db.commit()
    db.refresh(order)
    email_service.send_order_cancelled(order)
    return order


def cancel_order_by_customer(db: Session, order_id: uuid.UUID, user_id: str) -> Order:
    """A customer cancelling their own order.

    Ownership and which statuses they may cancel from are decided here; what
    cancelling *means* is deferred to cancel_order, which returns the stock.

    That delegation is the point. This used to set the status and commit
    directly, which was correct while stock moved at confirm - the statuses
    allowed here held none. Once every order held stock from creation, the
    duplicate quietly became an inventory leak: the admin path returned stock
    and this one destroyed it. One implementation cannot drift from itself.
    """
    order = _load_order(db, order_id)
    if order is None or order.user_id != user_id:
        raise ValueError("Order not found")
    if order.status not in CUSTOMER_CANCELLABLE_STATUSES:
        raise ValueError(f"Cannot cancel order in status {order.status.value}")

    return cancel_order(db, order_id)


# One page of the admin order list. Large enough that the common case is a
# single page, small enough that the response stays reasonable once there are
# thousands of orders.
DEFAULT_ORDER_PAGE_SIZE = 50
MAX_ORDER_PAGE_SIZE = 200


def list_all_orders(
    db: Session,
    status: OrderStatus | None = None,
    search: str | None = None,
    also_user_id: str | None = None,
    hold: str | None = None,
    limit: int = DEFAULT_ORDER_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[Order], int]:
    """One page of orders, and how many match in total.

    Returns the count as well as the rows because a page of results is not
    useful without knowing how many there are - the admin console cannot render
    "showing 50 of ?" or decide whether a next page exists otherwise.

    Searching happens here rather than in the browser. It used to be a client
    filter over every order ever fetched, which worked only because the list was
    unpaginated: the moment a page is a page, a client-side filter searches the
    current page and quietly reports nothing for everything else.
    """
    limit = max(1, min(limit, MAX_ORDER_PAGE_SIZE))
    offset = max(0, offset)

    filters = []
    if status:
        filters.append(Order.status == status)
    if hold:
        # Shared with the dashboard tiles that link here, so the count and the
        # list it points at cannot drift apart.
        filters.extend(hold_criteria(hold))

    if search and search.strip():
        term = f"%{search.strip()}%"
        # The same fields the client filter covered, so the search box behaves
        # as it did. ilike keeps it case-insensitive on Postgres; SQLite treats
        # LIKE as case-insensitive for ASCII anyway, so the tests agree.
        clauses = [
            Order.order_number.ilike(term),
            Order.customer_email.ilike(term),
            Order.shipping_name.ilike(term),
            Order.user_id.ilike(term),
            cast(Order.id, String).ilike(term),
        ]

        # An account resolved from the search term by the endpoint. Passed in
        # rather than looked up here so this stays a database function: the
        # search is exercised by the suite without mocking AWS, and Cognito
        # being unreachable degrades the search instead of breaking it.
        #
        # It matters because customer_email is where the customer asked mail to
        # go, and the whole reason support gets involved is that it was wrong.
        # Searching their real address would otherwise find nothing.
        if also_user_id:
            clauses.append(Order.user_id == also_user_id)

        filters.append(
            or_(
                *clauses,
            )
        )

    total = db.scalar(select(func.count()).select_from(Order).where(*filters)) or 0

    stmt = (
        select(Order)
        .where(*filters)
        .options(selectinload(Order.items))
        .order_by(*ORDER_LISTING_SORT)
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all()), total
