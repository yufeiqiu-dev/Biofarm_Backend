"""The saved basket.

Kept on the server rather than in the browser, because a basket in
`localStorage` belongs to a device and not to a person: fill one on a phone,
sign in on a laptop, and it is gone. It is also the groundwork for guest
checkout - see `owner_kind` on the model.

The lines store a variant and a quantity and nothing else. Names, prices and
images are read from the catalogue every time the basket is rendered, so a
repricing is reflected instead of sitting stale in a row. OrderItem denormalises
for the opposite reason: an order records what was actually bought and has to
survive the product being edited or deleted. A basket has no such duty.

The browser is local-first: it edits `localStorage` and only pushes here at a
handful of sync points (idle, tab hide, sign-in, sign-out - see
Biofarm_KnowledgeBase/documentation/designs/2026-09-08-local-first-cart-sync.md).
That is why `merge_cart` exists and reasons about timestamps at all - a device
that has been offline is not stale in the sense of "wrong", it is a second
writer whose changes have to be reconciled with whatever happened on the server
meanwhile, not blindly overwritten by either side.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.cart_item import MAX_LINE_QUANTITY, CartItem
from app.models.product_variant import ProductVariant

logger = logging.getLogger(__name__)

# What an owner_id means. A Cognito sub today; a guest cart token once genuine
# guest checkout lands, which is why the kind is stored rather than inferred
# from the shape of the identifier. A guest's cart today never reaches the
# server at all - see AddToCartButton and the design doc - so OWNER_GUEST is
# not yet written anywhere; it exists so the column does not need a migration
# when that lands.
OWNER_USER = "user"
OWNER_GUEST = "guest"

# MAX_LINE_QUANTITY lives on the model (cart_item.py), next to the data
# constraint it belongs with, so this module's clamp and the request schemas'
# bounds read from one place.

# The most recent tombstones a pull returns. A device that has been offline
# longer than this many add-then-remove churns is also long past the 90-day
# sweep, where there is no resurrection protection anyway - so newest-first,
# capped, keeps GET /cart's payload bounded without losing anything a plausibly
# offline device could still act on.
MAX_TOMBSTONES_RETURNED = 200

# A client_updated_at further ahead of server time than this is not a real edit
# moment - a skewed device clock. Such a line is skipped rather than clamped:
# clamping it to a server "now" that keeps advancing lets a stale device
# re-send the same future stamp forever and drift past every other device's
# genuine edits, and (once server time passes the stamp) resurrect purchased
# items. Ordinary skew is seconds; a device more than this far ahead has
# degraded cross-device cart sync until its clock is fixed - its local basket
# still works. Matched loosely to typical request-signing skew tolerance.
CLOCK_SKEW_GRACE = timedelta(minutes=2)

# How long a tombstone is kept before sweep_cart_tombstones removes it for good.
# Generous on purpose: the cost of keeping one too long is a slightly wider
# table, and the cost of sweeping one too soon is a genuine deletion silently
# undone the next time a very-offline device finally syncs.
TOMBSTONE_MAX_AGE_DAYS = 90


@dataclass(frozen=True)
class CartOwner:
    """Who a basket belongs to. Passed around as one value so a caller cannot
    supply an id without saying what kind of id it is."""

    kind: str
    id: str

    @staticmethod
    def user(sub: str) -> "CartOwner":
        return CartOwner(kind=OWNER_USER, id=sub)


@dataclass
class CartLine:
    """A basket line joined to the live catalogue."""

    variant: ProductVariant
    quantity: int
    #: True when the basket holds more than the shelf does.
    over_stock: bool
    #: What the server holds for this line's clock. Handed back to the client
    #: so it can adopt the server-accepted value - which may differ from what
    #: it sent, if a future timestamp was clamped.
    client_updated_at: datetime


@dataclass(frozen=True)
class CartTombstone:
    """A removed line, handed back to a pulling device so it can fold the
    deletion into its own merge. No catalogue join - a tombstone is compared
    against a local copy by its clock, never displayed."""

    variant_id: uuid.UUID
    client_updated_at: datetime


@dataclass(frozen=True)
class CartLineMerge:
    """One line from a device's local basket, offered up for reconciliation.

    Not the same shape as CartLine: this is what a client sends before it is
    known whether the variant still exists, or whether this line is even the
    newer of the two copies. `client_updated_at` is the device's own clock at
    the moment of the edit, not when this request happened to arrive - a line
    edited at 9:30pm by a phone that stayed offline until 11pm still carries
    9:30pm.
    """

    variant_id: uuid.UUID
    quantity: int
    client_updated_at: datetime
    #: A tombstone: this device removed the line at client_updated_at.
    deleted: bool = False


def _owned(owner: CartOwner):
    return (CartItem.owner_kind == owner.kind, CartItem.owner_id == owner.id)


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres tz-aware ones. Every
    comparison in this module treats client_updated_at as an absolute moment,
    so a naive value read back is assumed UTC - which is what was written."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def get_cart(db: Session, owner: CartOwner) -> list[CartLine]:
    """The basket, joined to the catalogue as it stands now.

    Tombstoned lines are excluded - they exist so a merge can tell a deletion
    from a device that never heard of the line, not to be shown to anyone.

    Quantities are reported as stored, not clamped to stock. Silently reducing
    someone's basket on a page load is how a customer ends up buying fewer than
    they meant to without noticing; the caller is told which lines exceed what
    is available and can say so.

    A variant that has been deleted takes its line with it, through the foreign
    key's ON DELETE CASCADE - so a discontinued product cannot linger in a
    basket as an id that resolves to nothing.
    """
    rows = list(
        db.scalars(
            select(CartItem)
            .where(*_owned(owner), CartItem.deleted_at.is_(None))
            .options(selectinload(CartItem.variant_ref).selectinload(ProductVariant.product))
            .order_by(CartItem.created_at, CartItem.id)
        )
    )

    lines: list[CartLine] = []
    for row in rows:
        variant = row.variant_ref
        if variant is None:
            continue
        lines.append(
            CartLine(
                variant=variant,
                quantity=row.quantity,
                over_stock=row.quantity > variant.stock,
                client_updated_at=_aware(row.client_updated_at),
            )
        )
    return lines


def get_cart_tombstones(db: Session, owner: CartOwner) -> list[CartTombstone]:
    """The owner's removed lines, for a client to weigh into its own merge.

    `get_cart` is live-only, which is right for display but leaves a pulling
    device unable to learn that a line it still holds live was deleted on
    another device: the variant is simply absent from the response, and a
    union merge reads absent as "unknown", not "gone". Handing back the
    tombstone with its clock lets the client compare it exactly as merge_cart
    does server-side, so the deletion propagates instead of the stale copy
    being resurrected and pushed back.

    No age cutoff is re-derived here: sweep_cart_tombstones is the one authority
    on how long a tombstone lives, and its age is CLI-configurable, so a second
    hardcoded window could silently keep a tombstone the sweep is retaining out
    of the response and leave a long-offline device re-pushing the line forever.

    But the count is capped at the newest MAX_TOMBSTONES_RETURNED - ranked by
    deleted_at, not client_updated_at. Those clocks can disagree a lot: a
    tombstone's deleted_at is always the server's own clock at the moment it
    recorded the deletion, but client_updated_at is the clamped *client* clock,
    which for a deletion synced late by a device that had been offline a while
    can be an old value despite the tombstone itself being brand new. Ranking
    by client_updated_at would then rank exactly the tombstone another device
    most needs to learn about as if it were stale, and cap it out first.

    A device offline through more deletions than the cap is also well past the
    90-day sweep, where there is no resurrection protection at all - so the cap
    loses nothing a plausibly-offline device could still act on, and it bounds
    GET /cart's payload for a customer with a long, busy history.
    """
    rows = db.execute(
        select(CartItem.variant_id, CartItem.client_updated_at)
        .where(*_owned(owner), CartItem.deleted_at.is_not(None))
        .order_by(CartItem.deleted_at.desc())
        .limit(MAX_TOMBSTONES_RETURNED)
    ).all()
    return [
        CartTombstone(
            variant_id=row.variant_id,
            client_updated_at=_aware(row.client_updated_at),
        )
        for row in rows
    ]


def merge_cart(db: Session, owner: CartOwner, incoming: list[CartLineMerge]) -> None:
    """Reconcile a device's whole local basket against the server's, line by
    line.

    Per line, not whole-basket-replace. Replace would mean whichever device
    pushed last wins outright - so a phone that was offline for an hour and
    syncs after a laptop, carrying one stale line, would clobber every change
    the laptop made meanwhile to every *other* line too. Merging line by line
    means the offline device contributes what it actually knows something newer
    about, and yields everywhere else.

    A variant this device's snapshot does not mention at all is left untouched.
    That is deliberate, not an oversight: it is how a line added by a *different*
    device, after this one's last sync, survives a push from a device that never
    learned about it. Only lines the caller explicitly includes - live or
    tombstoned - are ever compared.

    A future `client_updated_at` beyond ordinary clock skew is skipped, not
    clamped - see _apply_merge_line for why clamping a re-sent future stamp is
    worse than dropping it.

    One line that cannot be applied does not fail the rest. A lock timeout, a
    deadlock, a DataError - anything _apply_merge_line does not already handle -
    rolls back to a per-line savepoint and the loop moves on, so a single
    poisoned or unlucky line cannot 500 the whole background sync (sync_cart has
    no handler of its own).
    """
    # An empty list needs no special handling to be correct - the loop below is
    # simply a no-op and the commit that follows has nothing to persist - so
    # this is a short-circuit, not a guard: it exists to skip the pointless
    # clock read and commit for the idle debounce's common case of "nothing
    # changed". Mutation-tested and confirmed equivalent without it.
    if not incoming:
        return

    now = datetime.now(timezone.utc)
    # Sorted, and each row is locked FOR UPDATE below. Two merges for one
    # customer running at once - two devices whose idle-debounce syncs overlap -
    # otherwise both read the same `existing`, both judge their own edit newer,
    # and both issue an unconditional UPDATE: the row's final clock is then
    # whichever transaction committed last, not whichever client edit is
    # actually newer, silently dropping the newer change. Locking makes the
    # second merge re-read the first's result and compare against it. The sort
    # gives every caller the same lock order so they queue rather than deadlock.
    for line in sorted(incoming, key=lambda entry: str(entry.variant_id)):
        savepoint = db.begin_nested()
        try:
            _apply_merge_line(db, owner, line, now)
            savepoint.commit()
        except Exception:  # noqa: BLE001 - one line failing must not fail the sync
            savepoint.rollback()
            logger.warning(
                "cart sync: skipped a line that failed to apply for owner %s",
                owner.id,
                exc_info=True,
            )
    db.commit()


def _adopt(
    row: CartItem,
    *,
    quantity: int,
    deleted_at: Optional[datetime],
    client_updated_at: datetime,
) -> None:
    """Overwrite a line's mutable state in one place, so the merge's initial
    path, its post-race recovery, and set_line cannot drift apart on how a
    write is applied."""
    row.quantity = quantity
    row.deleted_at = deleted_at
    row.client_updated_at = client_updated_at


def _apply_merge_line(db: Session, owner: CartOwner, line: CartLineMerge, now: datetime) -> None:
    # _aware, for the same reason every value read from the DB gets it: a
    # comparison here treats the clock as an absolute moment. The frontend
    # sends toISOString() (always UTC "Z"), but an old client, a corrupted
    # localStorage value, or a non-browser caller can send a naive timestamp,
    # and `min(naive, aware)` / `naive <= aware` is a TypeError that 500s the
    # whole sync rather than failing one line.
    raw_client_updated_at = _aware(line.client_updated_at)

    # A clock further ahead than ordinary skew is not a real edit moment, and
    # clamping it does not help: a fire-and-forget client re-sends the same
    # future stamp on every sync, the clamp target ("now") keeps advancing, and
    # the line drifts past every other device's genuine edits - and once server
    # time passes the stamp, the now-"past" future value wins outright,
    # resurrecting purchased items. So it is skipped, like an unknown variant.
    # A device this skewed has degraded cross-device sync until its clock is
    # fixed; its local basket still works.
    if raw_client_updated_at > now + CLOCK_SKEW_GRACE:
        return

    # Within grace, a slightly-future stamp is clamped to now.
    client_updated_at = min(raw_client_updated_at, now)
    # Clamped, not rejected - the same as set_line. A single line whose quantity
    # a lowered MAX, an old client, or a corrupted localStorage value has put
    # out of range must not 422 the customer's whole background sync, when every
    # other bad-line case here (unknown variant, defunct variant) is skipped per
    # line. The floor is applied here too rather than in the schema: 0 means a
    # tombstone (deleted=True), a negative is meaningless, and both would
    # violate the table's CHECK (quantity > 0) if written through.
    quantity = min(max(line.quantity, 1), MAX_LINE_QUANTITY)

    # FOR UPDATE: without it two overlapping merges for this variant both read
    # the row here, both decide their edit wins, and the row's final state is
    # decided by commit order rather than by which client clock is newer - see
    # merge_cart. Locked in variant_id order (merge_cart sorts) so callers
    # queue rather than deadlock.
    existing = db.scalars(
        select(CartItem)
        .where(*_owned(owner), CartItem.variant_id == line.variant_id)
        .with_for_update()
    ).first()
    if existing is not None:
        # Strictly newer, not newer-or-equal: a tie changes nothing, and
        # writing on a tie would mean two devices with wall clocks that happen
        # to agree keep re-adopting each other's identical line forever.
        if client_updated_at <= _aware(existing.client_updated_at):
            return
        _adopt(
            existing,
            quantity=quantity,
            deleted_at=now if line.deleted else None,
            client_updated_at=client_updated_at,
        )
        return

    if line.deleted:
        # Nothing on the server to tombstone - there was never a row here for
        # this device's deletion to overwrite.
        return

    # Checked up front rather than left to the foreign key. FK ON DELETE
    # CASCADE means the insert below would fail anyway (verified against real
    # Postgres, where SQLite's laxer default enforcement would not have
    # proven anything) and _apply_merge_line's own IntegrityError handling
    # already recovers from that cleanly - so this line is redundant with the
    # constraint, not the only thing standing between a stale reference and a
    # bad row. It stays because a device that has been offline a while can
    # carry several now-defunct variants in one push, and failing fast avoids
    # a wasted insert/rollback/re-select for each.
    variant = db.get(ProductVariant, line.variant_id)
    if variant is None:
        return

    # A SAVEPOINT, not the outer transaction. merge_cart applies many lines in
    # one call and commits once at the end; a plain rollback here on a race
    # would undo every line already applied earlier in this same push, not just
    # this one.
    savepoint = db.begin_nested()
    try:
        db.add(
            CartItem(
                owner_kind=owner.kind,
                owner_id=owner.id,
                variant_id=line.variant_id,
                quantity=quantity,
                client_updated_at=client_updated_at,
            )
        )
        db.flush()
        savepoint.commit()
    except IntegrityError:
        # Another device's line for the same variant landed between the read
        # above and this insert. Re-run the comparison against what is there
        # now, rather than assuming either side should win.
        savepoint.rollback()
        existing = db.scalars(
            select(CartItem)
            .where(*_owned(owner), CartItem.variant_id == line.variant_id)
            .with_for_update()
        ).first()
        if existing is not None and client_updated_at > _aware(existing.client_updated_at):
            _adopt(
                existing,
                quantity=quantity,
                deleted_at=now if line.deleted else None,
                client_updated_at=client_updated_at,
            )


def set_line(db: Session, owner: CartOwner, variant_id: uuid.UUID, quantity: int) -> None:
    """Set one line to an absolute quantity, inserting, reviving, or removing as
    needed.

    Absolute rather than a delta, which is the opposite of the stock adjustment
    and deliberately so. Stock is a shared count where two writers must not lose
    each other's work; a basket has one owner, and "set this line to 3" is what
    a quantity box means.

    Not the sync path - the browser is local-first and calls merge_cart - but
    kept as a direct, single-line action: always wins whatever it touches,
    because "set this right now" is exactly as authoritative as an edit gets.
    """
    # Validated first, whatever the quantity. Short-circuiting to remove_line
    # before this made the same unknown id answer 200 with quantity 0 and 404
    # with quantity 1, so a caller could not use the endpoint to tell whether a
    # variant exists.
    variant = db.get(ProductVariant, variant_id)
    if variant is None:
        raise ValueError("Variant not found")

    if quantity <= 0:
        remove_line(db, owner, variant_id)
        return
    quantity = min(quantity, MAX_LINE_QUANTITY)
    now = datetime.now(timezone.utc)

    # FOR UPDATE, even though this write does not compare clocks - "set this
    # right now" always wins whatever it touches, by design. But an unlocked
    # read here is a stale snapshot: a purchase webhook's clear_bought_lines
    # could tombstone this exact row between the read and this function's own
    # write, and an unconditional `deleted_at=None` would silently resurrect
    # what was just bought. Locking makes this wait for that transaction and
    # then act on what is actually there, not what was there a moment ago -
    # still an absolute overwrite, just of the current row rather than a stale
    # copy of it.
    #
    # Not filtered on deleted_at: a tombstoned row is revived rather than
    # inserted alongside, which the unique constraint would refuse anyway.
    existing = db.scalars(
        select(CartItem)
        .where(*_owned(owner), CartItem.variant_id == variant_id)
        .with_for_update()
    ).first()
    if existing is not None:
        _adopt(existing, quantity=quantity, deleted_at=None, client_updated_at=now)
        db.commit()
        return

    db.add(
        CartItem(
            owner_kind=owner.kind,
            owner_id=owner.id,
            variant_id=variant_id,
            quantity=quantity,
            client_updated_at=now,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Two callers adding the same variant at once. The unique constraint is
        # what makes this safe to resolve rather than a lost line: whoever lost
        # the insert updates the row the winner created.
        db.rollback()
        row = db.scalars(
            select(CartItem)
            .where(*_owned(owner), CartItem.variant_id == variant_id)
            .with_for_update()
        ).first()
        if row is None:
            raise
        _adopt(row, quantity=quantity, deleted_at=None, client_updated_at=now)
        db.commit()


def remove_line(db: Session, owner: CartOwner, variant_id: uuid.UUID) -> None:
    """Tombstone one line. Idempotent: a line that is already gone stays gone,
    and its client_updated_at is left alone rather than bumped for no change in
    state."""
    now = datetime.now(timezone.utc)
    db.execute(
        update(CartItem)
        .where(*_owned(owner), CartItem.variant_id == variant_id, CartItem.deleted_at.is_(None))
        .values(deleted_at=now, client_updated_at=now)
    )
    db.commit()


def clear_bought_lines(
    db: Session, owner: CartOwner, bought: list[tuple[uuid.UUID, int]]
) -> None:
    """Take what an order bought out of the basket, without committing.

    Not the whole basket, and not whole lines either. Cross-device continuity is
    the point of keeping the basket on the server, which makes both failures
    ordinary rather than exotic: a customer checks out A on a laptop, adds B
    from their phone while the PaymentIntent is in flight, and the webhook
    lands. Emptying everything takes B, which nobody paid for - and deleting the
    whole A line takes the extra units of A they added in the meantime.

    So it decrements, and tombstones the line only when nothing is left of it -
    a real delete here, like remove_line, would let a stale offline device's
    later merge resurrect exactly what was just paid for. client_updated_at is
    bumped to now on both paths: a purchase is as authoritative an event as an
    edit gets, and if it did not win against an older client timestamp, a device
    that had been offline since before checkout could sync afterwards and put
    the bought units right back.

    A line the customer has already tombstoned is left alone. They checked out
    with it, then removed it before the payment landed; the removal is their
    latest word on it, and its own clock already protects it from a stale
    offline resurrection. Bumping the clock here on top of that would let the
    purchase outweigh a genuine re-add the customer made in between.
    """
    if not bought:
        return

    now = datetime.now(timezone.utc)
    # Sorted, and locked FOR UPDATE below - not just to avoid the deadlock two
    # webhooks for one customer with overlapping variants would otherwise hit
    # (the loser's commit failing means Stripe retries a webhook whose
    # CheckoutSession was never deleted - a second order for one payment), but
    # because the read-modify-write on quantity below would otherwise lose a
    # decrement: both webhooks read qty 5, one writes 3, the other writes 4 from
    # its stale read.
    for variant_id, quantity in sorted(bought, key=lambda entry: str(entry[0])):
        line = db.scalars(
            select(CartItem)
            .where(*_owned(owner), CartItem.variant_id == variant_id)
            .with_for_update()
        ).first()
        if line is None or line.deleted_at is not None:
            continue
        line.client_updated_at = now
        if line.quantity > quantity:
            line.quantity -= quantity
        else:
            line.deleted_at = now


def clear_bought_lines_best_effort(
    db: Session,
    owner: CartOwner,
    bought: list[tuple[uuid.UUID, int]],
    *,
    order_ref: Callable[[], object],
) -> None:
    """clear_bought_lines and commit, wrapped so it can never fail its caller.

    Both the webhook path and the bypass-checkout path call this after the order
    is already committed. A failure here - a lock timeout on a cart row the
    customer's other device is editing, most likely - must not become a 500
    that makes Stripe retry the webhook, or the checkout request retry: either
    is a second order for one payment. A basket that did not empty is a far
    smaller problem, and the customer can empty it.

    `order_ref` is a callable, not a plain value, and is only invoked here in
    the except branch - not by the caller beforehand. `order` is expired by the
    caller's commit, so an id or order number read from it is a SELECT; a
    caller capturing that eagerly on every call pays it on the happy path too,
    for a value only the failure path ever uses. Deferring it here means it is
    read at most once, and only when there is actually something to log - and
    it still cannot fail this function, since reading it happens inside the
    same guarded block as rollback and logging.
    """
    try:
        clear_bought_lines(db, owner, bought)
        db.commit()
    except Exception:  # noqa: BLE001 - the order matters, the basket does not
        # The recovery is itself guarded: if the connection is dead enough that
        # rollback, order_ref(), or even logging also raise, "can never fail
        # its caller" still has to hold.
        try:
            db.rollback()
            logger.exception("could not empty the basket after order %s", order_ref())
        except Exception:  # noqa: BLE001
            pass


def clear_cart(db: Session, owner: CartOwner) -> None:
    """Tombstone every live line outright.

    Order completion does not use this - it takes out only what was bought, via
    clear_bought_lines. This is the customer emptying the basket themselves.

    Locked in variant_id order, the same discipline merge_cart and
    clear_bought_lines follow. A bare bulk UPDATE would lock the owner's rows in
    scan order and could deadlock a clear_bought_lines running for the same
    customer at that moment - a purchase webhook landing while they empty the
    basket on another device.

    The ORDER BY holds even on a seq-scan plan: EXPLAIN puts LockRows *above*
    the Sort, so rows are locked in sorted order regardless of scan method
    (verified). And Postgres orders uuid the same way Python's sorted(str(...))
    does (verified), so this order matches the other paths'.
    """
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(CartItem)
        .where(*_owned(owner), CartItem.deleted_at.is_(None))
        .order_by(CartItem.variant_id)
        .with_for_update()
    ).all()
    for row in rows:
        row.deleted_at = now
        row.client_updated_at = now
    db.commit()


def sweep_cart_tombstones(db: Session, max_age_days: int = TOMBSTONE_MAX_AGE_DAYS) -> int:
    """Hard-delete tombstones old enough that no plausible offline device could
    still be carrying a pre-deletion copy to resurrect.

    Ages off `deleted_at`, not `updated_at`: a tombstone bumps both to the same
    moment, so either would answer this correctly today, but only deleted_at
    means what this function actually needs - how long the line has been gone.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    # is_not(None) is stated for the reader, not the database: SQL's own
    # three-valued logic already excludes a live row here, since NULL < cutoff
    # evaluates to NULL rather than true (verified against real Postgres) and a
    # WHERE clause only keeps rows where the condition is true. Mutation-tested
    # and confirmed equivalent without it; kept because "why does this only
    # touch tombstones" should not require knowing that.
    result = db.execute(
        delete(CartItem).where(CartItem.deleted_at.is_not(None), CartItem.deleted_at < cutoff)
    )
    db.commit()
    return result.rowcount or 0


def cart_subtotal(lines: list[CartLine]) -> Decimal:
    """Priced from the catalogue, so this is what the basket costs now."""
    return sum((line.variant.price * line.quantity for line in lines), Decimal("0"))
