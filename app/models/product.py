from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.tag import product_tags

if TYPE_CHECKING:
    from app.models.tag import Tag


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    cat_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    image_urls: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")

    variants: Mapped[List["ProductVariant"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )

    tags: Mapped[List["Tag"]] = relationship(
        secondary=product_tags,
        back_populates="products",
    )