from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ProductVariant(Base):
    __tablename__ = "product_variants"

    __table_args__ = (
        # Stock is decremented on confirm and restored on cancel, under a row
        # lock. If either of those ever gets the arithmetic wrong, this is what
        # turns overselling into a failed transaction rather than a negative
        # count nobody notices.
        CheckConstraint("stock >= 0", name="ck_variants_stock_not_negative"),
        CheckConstraint("price >= 0", name="ck_variants_price_not_negative"),
        CheckConstraint("size_value > 0", name="ck_variants_size_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        # Postgres does not index a foreign key for you. Without this, loading a
        # product's variants scans the table, and so does the CASCADE when a
        # product is deleted.
        index=True,
        nullable=False,
    )
    catalog_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    size_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    size_unit: Mapped[str] = mapped_column(String(50), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    product: Mapped["Product"] = relationship(back_populates="variants")