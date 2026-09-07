from typing import Optional

from pydantic import BaseModel


class QueueOut(BaseModel):
    """What is waiting on the admin right now."""

    to_confirm: int
    to_ship: int
    in_transit: int
    # None when nothing is waiting. The age matters more than the count: three
    # orders to confirm is a normal morning, one sitting four days is not.
    oldest_awaiting_hours: Optional[float] = None


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


class DashboardStatsOut(BaseModel):
    timezone: str
    generated_at: str
    queue: QueueOut
    volume: VolumeOut
    top_products: list[TopProductOut]
    catalogue: CatalogueOut
