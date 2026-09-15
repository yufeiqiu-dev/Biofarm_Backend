import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.product_variant import ProductVariant

# The ceiling on one line's quantity. Enforced in app code, not the DB (the
# table's own CHECK is only quantity > 0) - but it belongs with the data
# constraint rather than in a service, so cart_service's clamp and the request
# schemas' bounds read from one place. An unbounded quantity makes the tax call
# and the card authorisation absurd; nothing sensible orders more than this.
MAX_LINE_QUANTITY = 999


class CartItem(Base):
    """One line of a saved basket.

    A row per line rather than a JSON blob on a cart row, unlike
    CheckoutSession's `cart_json`. That column is a frozen snapshot of one
    payment attempt and is only ever written whole; this is a live basket that
    gets edited, and the difference matters twice over. The unique constraint on
    (owner_kind, owner_id, variant_id) makes "add the thing already in the
    basket" a single upsert rather than a read-modify-write, so two open tabs
    cannot lose each other's change. And the foreign key means a deleted variant
    takes its cart lines with it instead of leaving ids that resolve to nothing.

    Deliberately stores no name, price or image. Those are read from the
    catalogue when the basket is rendered, so a repricing shows up rather than
    sitting stale in a row. OrderItem denormalises for the opposite reason - an
    order is a record of what was actually bought, and must survive the product
    being edited or deleted.
    """

    __tablename__ = "cart_items"
    __table_args__ = (
        # One line per variant per basket. This is what upserts target.
        UniqueConstraint("owner_kind", "owner_id", "variant_id", name="uq_cart_owner_variant"),
        # A line of zero is a line that should have been deleted, and a negative
        # one is meaningless. Alembic does not compare CheckConstraints, so this
        # has to be written into the migration by hand as well.
        CheckConstraint("quantity > 0", name="ck_cart_items_quantity_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    """
    Who the basket belongs to, and what kind of owner that is.

    Two columns rather than one because guest checkout is the next feature and
    the two owners need different treatment. `owner_id` holds a Cognito sub
    today and will hold an anonymous cart token as well; `owner_kind` says
    which, so guest baskets can be swept on age while a signed-in customer's
    basket is kept indefinitely. Telling them apart after the fact would mean
    guessing from the shape of an identifier, which is exactly the kind of
    inference that stops being true quietly.
    """
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(nullable=False)

    """
    Two clocks, for two different jobs. See
    Biofarm_KnowledgeBase/documentation/designs/2026-09-08-local-first-cart-sync.md
    for the full reasoning.

    `client_updated_at` is when the customer actually made the change, as their
    own device's clock reports it - not when the row reached this database. That
    distinction is the whole point: the cart is edited offline (local-first,
    synced later), so a device reconnecting after an hour offline must not win a
    conflict just because its write happened to land last. Ordering by
    `updated_at` would do exactly that.

    `updated_at` stays a plain server timestamp, for sweeping and debugging -
    nothing about conflict resolution reads it.
    """
    client_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    """
    A tombstone, not a deleted row.

    A removed line has to be remembered as removed, or a merge cannot tell "this
    was deleted at 10pm" from "this device has never heard of this line" - and
    treats the second as license to resurrect the first. `quantity` is left
    alone rather than zeroed: the table's own CHECK (quantity > 0) forbids it,
    and there is no reason to throw the last known quantity away.

    Swept by app.jobs.cleanup after they are old enough that no plausible
    offline device could still be carrying a pre-deletion copy.
    """
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Named variant_ref rather than variant, so it cannot be mistaken for the
    # variant_id column when reading a query.
    variant_ref: Mapped["ProductVariant"] = relationship(lazy="raise")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        # Indexed for ad-hoc "what changed recently" ops queries. The tombstone
        # sweep filters on deleted_at, not this - that predicate is unindexed
        # and the daily sweep scans the table, which is deferred the same way
        # list-endpoint pagination is (see CLAUDE.md): cart_items is tiny, and a
        # partial index on deleted_at is speculative until it is not.
        index=True,
    )
