import uuid

from pydantic import BaseModel, Field

from app.schemas.numeric import Money


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


class CartLineIn(BaseModel):
    """An absolute quantity, not a change.

    The opposite of the stock adjustment, deliberately. Stock is a shared count
    where two writers must not lose each other's work; a basket has one owner,
    and "set this line to 3" is what a quantity box means.
    """

    quantity: int = Field(..., ge=0, le=999)
