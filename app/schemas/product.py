import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ProductVariantBase(BaseModel):
    catalog_id: str = Field(..., min_length=1, max_length=100)
    size_value: float = Field(..., gt=0)
    size_unit: str = Field(..., min_length=1, max_length=50)
    price: float = Field(..., ge=0)
    stock: int = Field(..., ge=0)


class ProductVariantCreate(ProductVariantBase):
    pass


class ProductVariantOut(ProductVariantBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class ProductVariantNestedUpdate(ProductVariantBase):
    id: Optional[uuid.UUID] = None


class ProductBase(BaseModel):
    cat_id: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=1)
    image_url: Optional[str] = None


class ProductCreate(ProductBase):
    variants: list[ProductVariantCreate] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    cat_id: Optional[str] = Field(default=None, min_length=1, max_length=50)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, min_length=1)
    image_url: Optional[str] = None
    variants: Optional[list[ProductVariantNestedUpdate]] = None


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    variants: list[ProductVariantOut] = Field(default_factory=list)