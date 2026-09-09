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
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.cart_item import CartItem
from app.models.product_variant import ProductVariant

# What an owner_id means. A Cognito sub today; a guest cart token once guest
# checkout lands, which is why the kind is stored rather than inferred from the
# shape of the identifier.
OWNER_USER = "user"
OWNER_GUEST = "guest"

# Nothing sensible orders more than this of one line, and an unbounded quantity
# is a way to make the tax call and the authorisation absurd.
MAX_LINE_QUANTITY = 999


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


def _owned(owner: CartOwner):
    return (CartItem.owner_kind == owner.kind, CartItem.owner_id == owner.id)


def get_cart(db: Session, owner: CartOwner) -> list[CartLine]:
    """The basket, joined to the catalogue as it stands now.

    Quantities are reported as stored, not clamped to stock. Silently reducing
    someone's basket on a page load is how a customer ends up buying fewer than
    they meant to without noticing; the caller is told which lines exceed what
    is available and can say so.

    A variant that has been deleted takes its line with it, through the foreign
    key's ON DELETE CASCADE - so a discontinued product cannot linger in a
    basket as an id that resolves to nothing, which is what the old localStorage
    cart did until checkout refused it.
    """
    rows = list(
        db.scalars(
            select(CartItem)
            .where(*_owned(owner))
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
            )
        )
    return lines


def set_line(db: Session, owner: CartOwner, variant_id: uuid.UUID, quantity: int) -> None:
    """Set one line to an absolute quantity, inserting or removing as needed.

    Absolute rather than a delta, which is the opposite of the stock adjustment
    and deliberately so. Stock is a shared count where two writers must not lose
    each other's work; a basket has one owner, and "set this line to 3" is what
    a quantity box means. Last write wins is the correct answer when both
    writers are the same person in two tabs.
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

    existing = db.scalars(
        select(CartItem).where(*_owned(owner), CartItem.variant_id == variant_id)
    ).first()
    if existing is not None:
        existing.quantity = quantity
        db.commit()
        return

    db.add(
        CartItem(
            owner_kind=owner.kind,
            owner_id=owner.id,
            variant_id=variant_id,
            quantity=quantity,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Two tabs adding the same variant at once. The unique constraint is
        # what makes this safe to resolve rather than a lost line: whoever lost
        # the insert updates the row the winner created.
        db.rollback()
        row = db.scalars(
            select(CartItem).where(*_owned(owner), CartItem.variant_id == variant_id)
        ).first()
        if row is None:
            raise
        row.quantity = quantity
        db.commit()


def remove_line(db: Session, owner: CartOwner, variant_id: uuid.UUID) -> None:
    """Idempotent: removing a line that is already gone is success, because the
    caller wanted it gone."""
    db.execute(delete(CartItem).where(*_owned(owner), CartItem.variant_id == variant_id))
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

    So it decrements, and removes the line only when nothing is left.
    """
    if not bought:
        return

    # Sorted, for the same reason _take_stock sorts the rows it locks: two
    # transactions taking the same rows in opposite orders deadlock. Two
    # webhooks for one customer with overlapping variants is enough, and the
    # loser's commit failing here means Stripe retries a webhook whose
    # CheckoutSession was never deleted - a second order for one payment.
    for variant_id, quantity in sorted(bought, key=lambda entry: str(entry[0])):
        line = db.scalars(
            select(CartItem).where(*_owned(owner), CartItem.variant_id == variant_id)
        ).first()
        if line is None:
            continue
        if line.quantity > quantity:
            line.quantity -= quantity
        else:
            db.delete(line)


def clear_cart(db: Session, owner: CartOwner) -> None:
    """Empty the basket outright.

    Order completion does not use this - it takes out only what was bought, via
    clear_bought_lines. This is the customer emptying it themselves, which is
    its own transaction.
    """
    db.execute(delete(CartItem).where(*_owned(owner)))
    db.commit()


def cart_subtotal(lines: list[CartLine]) -> Decimal:
    """Priced from the catalogue, so this is what the basket costs now."""
    return sum((line.variant.price * line.quantity for line in lines), Decimal("0"))
