import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.numeric import Measure, Money
from app.schemas.tag import TagOut


class ProductVariantBase(BaseModel):
    catalog_id: str = Field(..., min_length=1, max_length=100)
    size_value: Measure
    size_unit: str = Field(..., min_length=1, max_length=50)
    price: Money
    stock: int = Field(..., ge=0)


class ProductVariantCreate(ProductVariantBase):
    pass


class ProductVariantOut(ProductVariantBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class ProductVariantNestedUpdate(ProductVariantBase):
    """A variant inside a product PUT: an existing one by id, or a new one.

    `stock` is write-once here. A new variant needs an opening count, but an
    existing one's stock does not belong in this payload at all: the form is
    read-modify-write over a whole product, so the number it sends was read
    before the admin started typing. Every sale in between is overwritten by it -
    an admin correcting a typo in a description silently restored stock that had
    been sold, and the shelf and the system disagreed with nothing logged.

    Rejected rather than ignored, so a client that has not moved to the restock
    endpoint is told, instead of watching its writes vanish.
    """

    id: Optional[uuid.UUID] = None
    stock: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _stock_only_on_new_variants(self) -> "ProductVariantNestedUpdate":
        if self.id is None and self.stock is None:
            raise ValueError("A new variant needs an opening stock count")
        if self.id is not None and self.stock is not None:
            raise ValueError(
                "Stock cannot be set here - use the restock endpoint, which "
                "applies a change rather than overwriting the count"
            )
        return self


class VariantStockAdjustment(BaseModel):
    """A signed change to a variant's stock, not a new value.

    A delta because two writers cannot lose each other's work with one: the
    count is never read into the client and sent back. Signed because the
    correction an admin needs after a miscount or breakage is the same operation
    as a delivery, and a separate "set" endpoint would reintroduce exactly the
    overwrite this exists to remove.
    """

    delta: int = Field(..., description="Added to the current stock; may be negative")
    reason: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _must_change_something(self) -> "VariantStockAdjustment":
        if self.delta == 0:
            raise ValueError("A stock adjustment must be non-zero")
        return self


class ProductBase(BaseModel):
    cat_id: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=1)


class ProductCreate(ProductBase):
    tag_ids: list[uuid.UUID] = Field(default_factory=list)
    variants: list[ProductVariantCreate] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    cat_id: Optional[str] = Field(default=None, min_length=1, max_length=50)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, min_length=1)
    tag_ids: Optional[list[uuid.UUID]] = None
    image_urls: Optional[list[str]] = None
    variants: Optional[list[ProductVariantNestedUpdate]] = None


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    tags: list[TagOut] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    variants: list[ProductVariantOut] = Field(default_factory=list)
