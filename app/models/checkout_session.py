import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CheckoutSession(Base):
    __tablename__ = "checkout_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    stripe_pi_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    cart_json: Mapped[str] = mapped_column(Text, nullable=False)
    shipping_json: Mapped[str] = mapped_column(Text, nullable=False)
    customer_email: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    tax_amount_cents: Mapped[int] = mapped_column(nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        # cleanup_stale_checkout_sessions sweeps by age, so this is the column
        # it filters on - without an index that sweep scans the table.
        index=True,
    )
