from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class OrderStatus(enum.Enum):
    pending = "pending"
    awaiting_fulfillment = "awaiting_fulfillment"
    confirmed = "confirmed"
    shipped = "shipped"
    delivered = "delivered"
    cancelled = "cancelled"


class Order(Base):
    __tablename__ = "orders"

    __table_args__ = (
        # The admin list is "newest first, optionally one status". Leading with
        # status lets the same index serve both the filtered tabs and the
        # unfiltered one, and having created_at in it means the page does not
        # sort the whole table to return fifty rows.
        Index("ix_orders_status_created_at", "status", "created_at"),
        # The database should refuse what the application already refuses.
        # Money is never negative here: a refund is a Stripe operation, not a
        # negative order.
        CheckConstraint("total_amount >= 0", name="ck_orders_total_not_negative"),
        CheckConstraint("tax_amount >= 0", name="ck_orders_tax_not_negative"),
        CheckConstraint("shipping_amount >= 0", name="ck_orders_shipping_not_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # A random, customer-facing identifier - see services/order_numbers.py.
    # Not sequential and not an ordering key: sort by created_at.
    order_number: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[OrderStatus] = mapped_column(
        # create_constraint is False by default since SQLAlchemy 1.4, so this
        # was a plain varchar(50) that would accept any string at all. The
        # application validates on the way in; the database now does too.
        SAEnum(OrderStatus, native_enum=False, length=50, create_constraint=True),
        nullable=False,
        default=OrderStatus.pending,
    )
    stripe_payment_intent_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    shipping_name: Mapped[str] = mapped_column(String(255), nullable=False)
    shipping_phone: Mapped[str] = mapped_column(String(50), nullable=False)
    shipping_address1: Mapped[str] = mapped_column(String(255), nullable=False)
    shipping_address2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shipping_city: Mapped[str] = mapped_column(String(100), nullable=False)
    shipping_state: Mapped[str] = mapped_column(String(2), nullable=False)
    shipping_zip: Mapped[str] = mapped_column(String(20), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    card_brand: Mapped[str] = mapped_column(String(50), nullable=False, server_default="")
    card_last4: Mapped[str] = mapped_column(String(4), nullable=False, server_default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, server_default="0.00")
    # What the customer paid to have it sent. Stored rather than recomputed: the
    # rate can change, and an old order must still add up to what was charged.
    shipping_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        # server_default fills existing rows and any INSERT that omits the
        # column; `default` supplies the value at flush time. Neither populates
        # the *attribute* of an Order that has not been flushed - it reads None
        # until then, which is why email_service treats a missing amount as
        # zero rather than trusting nullable=False.
        default=Decimal("0.00"),
        server_default="0.00",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    __table_args__ = (
        # A line for none of something is not a line. Nothing in the application
        # writes one, which is exactly why it is cheap to guarantee.
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_price_not_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product_variants.id", ondelete="SET NULL"),
        nullable=True,
        # Unindexed, the SET NULL on a variant delete scans every order item
        # ever written.
        index=True,
    )
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    variant_label: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
