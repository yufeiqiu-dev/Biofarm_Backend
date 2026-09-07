"""What the admin console's dashboard reports.

Aggregated in SQL rather than in the browser. The admin order list was moved to
server-side paging precisely so the console stops fetching every order; a
dashboard that pulled them all down to count them would walk straight back into
that, and would do it on the page most likely to be left open all day.

Separate grouped queries rather than one clever conditional aggregate: the suite
runs on SQLite and the server on Postgres, and `FILTER (WHERE ...)` is not
portable between them. At this size the difference is unmeasurable and the
queries stay legible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant

# Not a new number. productSummary.ts already uses 5 to decide when the
# storefront says "n left" in its low-stock tone, and the admin must not
# disagree with the shop about what "low" means. Returned in the payload so the
# dashboard labels itself from this value instead of keeping a second copy.
LOW_STOCK_THRESHOLD = 5

# Enough to act on. A dashboard listing forty variants is a spreadsheet, and the
# count beside it says whether the list is the whole story.
LOW_STOCK_LIMIT = 10
TOP_PRODUCTS_LIMIT = 5

# Statuses that mean the order is still real. A cancelled order is not work and
# not a sale, and counting it would overstate both.
LIVE_STATUSES = (
    OrderStatus.pending,
    OrderStatus.awaiting_fulfillment,
    OrderStatus.confirmed,
    OrderStatus.shipped,
    OrderStatus.delivered,
)


def _business_zone() -> ZoneInfo:
    """The configured zone, falling back rather than failing.

    A dashboard is not worth a 500 over a typo in a setting, and an unknown zone
    name is exactly the kind of thing that reaches a task definition unnoticed.
    """
    try:
        return ZoneInfo(get_settings().business_timezone)
    except Exception:  # noqa: BLE001 - any zoneinfo failure means the same thing
        return ZoneInfo("UTC")


def window_starts(now: datetime | None = None) -> dict[str, datetime]:
    """The UTC instants each window begins at.

    Computed by taking local midnight in the business zone and converting back,
    so "today" is the admin's day rather than UTC's. The comparison itself stays
    in UTC because that is what the column holds.

    The 7 and 30 day windows are calendar days *including* today, not rolling
    168-hour spans - mixing the two would make "today" and "last 7 days"
    disagree about where a day begins.
    """
    zone = _business_zone()
    local_now = (now or datetime.now(tz=timezone.utc)).astimezone(zone)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    return {
        "today": local_midnight.astimezone(timezone.utc),
        "last_7_days": (local_midnight - timedelta(days=6)).astimezone(timezone.utc),
        "last_30_days": (local_midnight - timedelta(days=29)).astimezone(timezone.utc),
    }


def _queue(db: Session) -> dict[str, Any]:
    """How many orders are waiting on the admin, and how long the oldest has been.

    The age is the point. Three orders to confirm is a normal morning; one that
    has been sitting four days is a customer wondering whether the shop is real,
    and a bare count cannot tell those apart.
    """
    counts = dict(
        db.execute(
            select(Order.status, func.count())
            .where(Order.status.in_(LIVE_STATUSES))
            .group_by(Order.status)
        ).all()
    )

    oldest = db.scalar(
        select(func.min(Order.created_at)).where(
            Order.status == OrderStatus.awaiting_fulfillment
        )
    )

    oldest_age_hours = None
    if oldest is not None:
        # SQLite hands back a naive datetime; Postgres a tz-aware one.
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        delta = datetime.now(tz=timezone.utc) - oldest
        oldest_age_hours = round(delta.total_seconds() / 3600, 1)

    return {
        "to_confirm": counts.get(OrderStatus.awaiting_fulfillment, 0),
        "to_ship": counts.get(OrderStatus.confirmed, 0),
        "in_transit": counts.get(OrderStatus.shipped, 0),
        "oldest_awaiting_hours": oldest_age_hours,
    }


def _daily_series(db: Session, since: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    """Orders per local day for the window, with a running total.

    The cumulative figure counts from the shop's first order, not from the start
    of the window - the point of it is the growth curve, and one that restarts
    every month shows nothing.

    Bucketed in Python rather than in SQL. Grouping by *local* date needs a
    timezone conversion inside the query, and the syntax for that is not portable
    between the SQLite the suite runs on and the Postgres the server uses. Only
    one indexed column is fetched, and only for the window, so even a busy shop
    is a few thousand timestamps.
    """
    today_local = datetime.now(tz=timezone.utc).astimezone(zone).date()

    rows = db.scalars(
        select(Order.created_at)
        .where(Order.status.in_(LIVE_STATUSES), Order.created_at >= since)
        .order_by(Order.created_at)
    ).all()

    counts: dict[str, int] = {}
    for created in rows:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        # Folded into today rather than dropped. The loop below stops at today,
        # so a bucket beyond it would be built and discarded - and the curve's
        # end would silently disagree with the all-time tile rendered directly
        # above the chart. Excluding such an order from the query instead leaves
        # it in neither the buckets nor the pre-window total, which is the same
        # disagreement moved. Reachable through clock skew between instances, or
        # an import that dates an order forward.
        local_date = min(created.astimezone(zone).date(), today_local)
        key = local_date.isoformat()
        counts[key] = counts.get(key, 0) + 1

    # Everything before the window, so the line starts where the business
    # actually is rather than at zero.
    running = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status.in_(LIVE_STATUSES), Order.created_at < since)
        )
        or 0
    )

    first_day = since.astimezone(zone).date()
    today = today_local

    series: list[dict[str, Any]] = []
    day = first_day
    while day <= today:
        orders = counts.get(day.isoformat(), 0)
        running += orders
        series.append({"date": day.isoformat(), "orders": orders, "cumulative": running})
        day += timedelta(days=1)

    return series


def _queue_value(db: Session) -> Decimal:
    """What the unshipped queue is worth, pre-tax.

    Named for the queue, not for "awaiting", because it sums awaiting_fulfillment
    *and* confirmed - everything not yet shipped. Sitting in QueueOut beside
    `to_confirm` and `oldest_awaiting_hours`, both of which mean
    awaiting_fulfillment alone, a name like `awaiting_value` reads as the value
    of that one tile: an admin with 3 to confirm and 40 to ship would take it for
    the 3.

    The to-do list priced, so an admin can tell an afternoon from ten minutes.
    Deliberately not called revenue and deliberately not a payout figure: Stripe
    is authoritative for money, and anything computed here drifts from it on
    fees, refunds and disputes. Sales tax is excluded because it is not the
    shop's.

    Returns Decimal, not float: CLAUDE.md requires the Money alias for any
    decimal crossing the API, and this was the one place a Numeric(10, 2) column
    reached it as a float.
    """
    total = db.scalar(
        select(func.coalesce(func.sum(Order.total_amount), 0)).where(
            Order.status.in_((OrderStatus.awaiting_fulfillment, OrderStatus.confirmed))
        )
    )
    return Decimal(total or 0)


def _volume(db: Session, starts: dict[str, datetime]) -> dict[str, int]:
    """Orders placed per window, cancellations excluded."""
    volume = {
        key: db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status.in_(LIVE_STATUSES), Order.created_at >= start)
        )
        or 0
        for key, start in starts.items()
    }
    volume["all_time"] = (
        db.scalar(
            select(func.count()).select_from(Order).where(Order.status.in_(LIVE_STATUSES))
        )
        or 0
    )
    return volume


def _top_products(db: Session, since: datetime) -> list[dict[str, Any]]:
    """Units sold per product over the window.

    Grouped on OrderItem.product_name rather than joining back to Product,
    because the name is denormalised onto the item - so the figures survive a
    product being renamed, repriced, or deleted outright, which is the whole
    point of historical reporting.
    """
    rows = db.execute(
        select(
            OrderItem.product_name,
            func.sum(OrderItem.quantity).label("units"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.status.in_(LIVE_STATUSES), Order.created_at >= since)
        .group_by(OrderItem.product_name)
        .order_by(func.sum(OrderItem.quantity).desc(), OrderItem.product_name)
        .limit(TOP_PRODUCTS_LIMIT)
    ).all()

    return [{"product_name": name, "units": int(units or 0)} for name, units in rows]


def _catalogue(db: Session) -> dict[str, Any]:
    """What is running out, and what cannot be bought at all."""
    low_rows = db.execute(
        select(
            ProductVariant.id,
            ProductVariant.catalog_id,
            ProductVariant.stock,
            ProductVariant.size_value,
            ProductVariant.size_unit,
            Product.id,
            Product.name,
        )
        .join(Product, Product.id == ProductVariant.product_id)
        .where(ProductVariant.stock <= LOW_STOCK_THRESHOLD)
        .order_by(ProductVariant.stock, Product.name)
        .limit(LOW_STOCK_LIMIT)
    ).all()

    low_stock = [
        {
            "variant_id": str(variant_id),
            "catalog_id": catalog_id,
            "stock": stock,
            "variant_label": f"{size_value} {size_unit}",
            "product_id": str(product_id),
            "product_name": product_name,
        }
        for variant_id, catalog_id, stock, size_value, size_unit, product_id, product_name in low_rows
    ]

    low_total = (
        db.scalar(
            select(func.count())
            .select_from(ProductVariant)
            .where(ProductVariant.stock <= LOW_STOCK_THRESHOLD)
        )
        or 0
    )
    out_of_stock = (
        db.scalar(
            select(func.count()).select_from(ProductVariant).where(ProductVariant.stock == 0)
        )
        or 0
    )

    # A product with no variant is filtered out of list_public_products, so it
    # is silently unbuyable - present in the admin console, absent from the shop,
    # and nothing anywhere says so.
    invisible_rows = db.execute(
        select(Product.id, Product.cat_id, Product.name)
        .outerjoin(ProductVariant, ProductVariant.product_id == Product.id)
        .group_by(Product.id, Product.cat_id, Product.name)
        .having(func.count(ProductVariant.id) == 0)
        .order_by(Product.name)
    ).all()

    return {
        "products": db.scalar(select(func.count()).select_from(Product)) or 0,
        "variants": db.scalar(select(func.count()).select_from(ProductVariant)) or 0,
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
        "low_stock_total": low_total,
        "low_stock": low_stock,
        "out_of_stock": out_of_stock,
        "invisible_products": [
            {"id": str(pid), "cat_id": cat_id, "name": name}
            for pid, cat_id, name in invisible_rows
        ],
    }


def get_dashboard_stats(db: Session) -> dict[str, Any]:
    """Everything the dashboard renders, in one response."""
    starts = window_starts()

    queue = _queue(db)
    queue["queue_value"] = _queue_value(db)

    return {
        # The zone actually resolved, not the setting. _business_zone falls back
        # to UTC on a bad value, and reporting the raw setting would have the
        # footer claim "Dates in America/New_Yrok" while every bar was bucketed
        # in UTC - the label asserting exactly what is not true.
        "timezone": str(_business_zone()),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "queue": queue,
        "volume": _volume(db, starts),
        "daily": _daily_series(db, starts["last_30_days"], _business_zone()),
        "top_products": _top_products(db, starts["last_30_days"]),
        "catalogue": _catalogue(db),
    }
