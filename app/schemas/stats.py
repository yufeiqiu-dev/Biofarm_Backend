from typing import Optional

from decimal import Decimal

from pydantic import BaseModel

from app.schemas.numeric import Money


class QueueOut(BaseModel):
    """What is waiting on the admin right now."""

    to_confirm: int
    to_ship: int
    in_transit: int
    # None when nothing is waiting. The age matters more than the count: three
    # orders to confirm is a normal morning, one sitting four days is not.
    oldest_awaiting_hours: Optional[float] = None
    # Everything unshipped, priced pre-tax: awaiting_fulfillment and confirmed
    # together. Named for the queue rather than for "awaiting" because the two
    # fields above mean awaiting_fulfillment alone - an admin with 3 to confirm
    # and 40 to ship would otherwise read this as the value of the 3.
    #
    # Money, not float: the alias CLAUDE.md requires for a decimal crossing the
    # API. It still serialises as a JSON number.
    queue_value: Money = Decimal("0")


class VolumeOut(BaseModel):
    today: int
    last_7_days: int
    last_30_days: int
    all_time: int


class TopProductOut(BaseModel):
    product_name: str
    units: int


class LowStockOut(BaseModel):
    variant_id: str
    catalog_id: str
    stock: int
    variant_label: str
    product_id: str
    product_name: str


class InvisibleProductOut(BaseModel):
    id: str
    cat_id: str
    name: str


class CatalogueOut(BaseModel):
    products: int
    variants: int
    # Sent so the dashboard labels itself from the server's number rather than
    # keeping a second copy of the threshold to drift.
    low_stock_threshold: int
    low_stock_total: int
    low_stock: list[LowStockOut]
    out_of_stock: int
    invisible_products: list[InvisibleProductOut]


class DailyPointOut(BaseModel):
    date: str
    orders: int
    # Counted from the shop's first order, not the window's start: a growth
    # curve that restarts every month shows nothing.
    cumulative: int


class DashboardStatsOut(BaseModel):
    timezone: str
    generated_at: str
    queue: QueueOut
    volume: VolumeOut
    daily: list[DailyPointOut]
    top_products: list[TopProductOut]
    catalogue: CatalogueOut
