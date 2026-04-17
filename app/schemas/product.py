from __future__ import annotations

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


class ProductVariantUpdate(BaseModel):
    catalog_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    size_value: Optional[float] = Field(default=None, gt=0)
    size_unit: Optional[str] = Field(default=None, min_length=1, max_length=50)
    price: Optional[float] = Field(default=None, ge=0)
    stock: Optional[int] = Field(default=None, ge=0)


class ProductVariantOut(ProductVariantBase):
    model_config = ConfigDict(from_attributes=True)

    id: str


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


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    variants: list[ProductVariantOut] = Field(default_factory=list)