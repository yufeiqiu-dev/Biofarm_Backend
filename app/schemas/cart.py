import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.cart_item import MAX_LINE_QUANTITY
from app.schemas.numeric import Money

# The most lines one sync push may carry - live and tombstoned together. A real
# basket is a handful; even a heavy add-then-remove history is bounded by the
# 90-day tombstone sweep and the client's own prune. This is the ceiling on how
# long a single PUT /cart can hold row locks while merge_cart loops, so it is
# generous but finite. A payload past it is corruption or abuse, and a 422 is
# the right answer there.
MAX_SYNC_LINES = 500


class CartLineOut(BaseModel):
    """A basket line, priced and described from the catalogue as it stands.

    Everything but the quantity is read live rather than stored on the line, so
    a repricing or a rename shows up in the basket instead of the customer
    seeing what the product cost when they added it.
    """

    variant_id: uuid.UUID
    product_id: uuid.UUID
    name: str
    catalog_number: str
    size_label: str
    image_url: str = ""
    unit_price: Money
    quantity: int
    #: What the shelf holds now, so the client can offer a sensible maximum.
    available: int
    #: True when the basket asks for more than `available`.
    over_stock: bool
    #: The timestamp the server accepted for this line. Returned so the client
    #: can adopt it as the local line's own clientUpdatedAt after a sync,
    #: rather than trusting a value it sent minutes ago and a clamp may have
    #: changed.
    client_updated_at: datetime


class CartTombstoneOut(BaseModel):
    """A removed line, handed back so a pulling device can fold the deletion
    into its own merge rather than resurrecting a copy it still holds live.

    No price or name: a tombstone is compared by its clock, never displayed.
    """

    variant_id: uuid.UUID
    client_updated_at: datetime


class CartOut(BaseModel):
    items: list[CartLineOut] = Field(default_factory=list)
    subtotal: Money
    """
    Lines the customer cannot currently have in full.

    Reported rather than silently trimmed: quietly reducing a basket on a page
    load is how someone ends up buying fewer than they meant to and only finding
    out from the receipt.
    """
    unavailable: list[str] = Field(default_factory=list)
    #: Lines removed on some device, within the window a very-late sync could
    #: still legitimately carry a pre-deletion copy of. The client merges these
    #: by clientUpdatedAt exactly as it merges live lines, so a deletion made
    #: on one device is not undone by another that still holds the line live.
    deleted_lines: list[CartTombstoneOut] = Field(default_factory=list)


class CartLineIn(BaseModel):
    """An absolute quantity, not a change.

    The opposite of the stock adjustment, deliberately. Stock is a shared count
    where two writers must not lose each other's work; a basket has one owner,
    and "set this line to 3" is what a quantity box means.
    """

    quantity: int = Field(..., ge=0, le=MAX_LINE_QUANTITY)


class CartMergeLineIn(BaseModel):
    """One line from a device's local, possibly-offline basket, offered up for
    reconciliation rather than asserted outright.

    `client_updated_at` is the device's own clock at the moment of the edit -
    not now, and not when this request happens to arrive. A line last touched at
    9:30pm by a phone that only reconnects at 11pm still carries 9:30pm; the
    server is what decides whether that beats whatever else has happened to this
    variant since.
    """

    variant_id: uuid.UUID
    # No bounds here, on purpose. This is a background sync of a whole basket,
    # and one line whose quantity a lowered maximum, an old client, or a
    # corrupted localStorage snapshot has put out of range must not 422 the
    # entire request - merge_cart skips or clamps every other kind of bad line
    # per-line, and a schema floor/ceiling that runs first defeats that.
    # _apply_merge_line clamps to [1, MAX_LINE_QUANTITY]; 0 means a tombstone
    # (deleted=True), which the clamp handles rather than the schema rejecting.
    quantity: int
    client_updated_at: datetime
    #: True when this device removed the line at client_updated_at.
    deleted: bool = False


class CartMergeRequest(BaseModel):
    """A device's whole local basket, pushed for reconciliation.

    Not a whole-basket replace. A variant this list does not mention at all is
    left untouched on the server - that is how a line another device added
    since this one's last sync survives being pushed by a device that never
    learned about it. Only lines actually present here, live or tombstoned, are
    ever compared.

    Capped at MAX_SYNC_LINES: merge_cart loops one locking SELECT per line in a
    single transaction, so the list length is the ceiling on how long that
    transaction holds row locks. A real basket is nowhere near it.
    """

    items: list[CartMergeLineIn] = Field(default_factory=list, max_length=MAX_SYNC_LINES)
