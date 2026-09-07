"""The admin dashboard's figures.

The timezone boundary is first deliberately. It is the whole reason
BUSINESS_TIMEZONE exists and it is invisible in any test that happens to run at
midday - which is every test that does not construct the moment itself.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.models.order import OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.services.stats_service import window_starts
from app.tests.test_orders import make_order, make_product_with_variant


# --- the windows -------------------------------------------------------------

def test_an_evening_order_belongs_to_today_not_tomorrow():
    """8pm in New York is already tomorrow in UTC.

    created_at is stored in UTC, so a naive "since midnight UTC" would push an
    evening order into tomorrow's figures - the dashboard disagreeing with the
    admin about what day it is, every evening, which is when a one-person shop
    does its admin.
    """
    evening_in_new_york = datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc)  # 8pm EDT on the 15th

    starts = window_starts(now=evening_in_new_york)

    # Midnight New York on the 15th is 04:00 UTC on the 15th.
    assert starts["today"] == datetime(2026, 3, 15, 4, 0, tzinfo=timezone.utc)

    # An order placed at 7pm New York (23:00 UTC on the 15th) is today's.
    order_placed = datetime(2026, 3, 15, 23, 0, tzinfo=timezone.utc)
    assert order_placed >= starts["today"], "an evening order fell out of today"

    # And the naive version would have got it wrong, which is what this guards.
    naive_utc_midnight = evening_in_new_york.replace(hour=0, minute=0)
    assert order_placed < naive_utc_midnight


def test_the_windows_are_calendar_days_and_survive_a_clock_change():
    """Each window begins at local midnight, N days back - and the arithmetic is
    done in local time before converting, not after.

    29 days before 16 March 2026 is 15 February, which is on the other side of
    the DST change on the 8th. Local midnight is UTC-5 there and UTC-4 today, so
    subtracting a timedelta from the UTC instant lands at 1am local instead of
    midnight, and the window silently loses an hour of its first day.

    This test asserted equal offsets at first and failed - correctly. The code
    was right; the assumption was not.
    """
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 16, 16, 0, tzinfo=timezone.utc)  # noon EDT

    starts = window_starts(now=now)

    for key, days_back in (("today", 0), ("last_7_days", 6), ("last_30_days", 29)):
        local = starts[key].astimezone(zone)
        assert (local.hour, local.minute, local.second) == (0, 0, 0), (
            f"{key} does not begin at local midnight: {local}"
        )
        assert local.date() == date(2026, 3, 16) - timedelta(days=days_back)

    # The naive version, kept as the thing this guards against.
    assert starts["last_30_days"] != starts["today"] - timedelta(days=29)


def test_an_unknown_timezone_falls_back_rather_than_failing(monkeypatch):
    """A dashboard is not worth a 500 over a typo in a task definition."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "business_timezone", "Not/AZone", raising=False)
    starts = window_starts(now=datetime(2026, 3, 16, 12, 0, tzinfo=timezone.utc))

    assert starts["today"] == datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc)


# --- the queue ---------------------------------------------------------------

def test_the_queue_counts_what_is_waiting_on_the_admin(admin_client: TestClient, db_session):
    make_order(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order(db_session, status=OrderStatus.confirmed)
    make_order(db_session, status=OrderStatus.shipped)

    queue = admin_client.get("/api/v1/admin/stats").json()["queue"]

    assert queue["to_confirm"] == 2
    assert queue["to_ship"] == 1
    assert queue["in_transit"] == 1


def test_a_cancelled_order_is_not_work(admin_client: TestClient, db_session):
    make_order(db_session, status=OrderStatus.cancelled)

    body = admin_client.get("/api/v1/admin/stats").json()

    assert body["queue"]["to_confirm"] == 0
    assert body["volume"]["all_time"] == 0, "a cancelled order counted as a sale"


def test_the_queue_reports_how_long_the_oldest_has_waited(admin_client: TestClient, db_session):
    """A count of one says nothing about whether it arrived this morning or on
    Friday."""
    order, _ = make_order(db_session, status=OrderStatus.awaiting_fulfillment)
    order.created_at = datetime.now(tz=timezone.utc) - timedelta(hours=50)
    db_session.commit()

    queue = admin_client.get("/api/v1/admin/stats").json()["queue"]

    assert queue["oldest_awaiting_hours"] is not None
    assert 49 < queue["oldest_awaiting_hours"] < 51


def test_nothing_waiting_reports_no_age(admin_client: TestClient, db_session):
    assert admin_client.get("/api/v1/admin/stats").json()["queue"]["oldest_awaiting_hours"] is None


# --- what is running out -----------------------------------------------------

def test_low_stock_is_listed_not_merely_counted(admin_client: TestClient, db_session):
    """The admin's next action is to reorder a specific thing, so a number alone
    just makes them go and find it."""
    make_product_with_variant(db_session, "LOW-1", stock=2)
    make_product_with_variant(db_session, "PLENTY-1", stock=50)

    catalogue = admin_client.get("/api/v1/admin/stats").json()["catalogue"]

    assert catalogue["low_stock_total"] == 1
    assert len(catalogue["low_stock"]) == 1
    entry = catalogue["low_stock"][0]
    assert entry["catalog_id"] == "LOW-1-V1"
    assert entry["stock"] == 2
    assert entry["product_id"], "no way to navigate to the product that needs reordering"


def test_the_threshold_is_reported_so_the_page_need_not_hardcode_it(
    admin_client: TestClient, db_session
):
    """The storefront already calls 5 'low'. The admin and the shop must not
    disagree about what the word means."""
    catalogue = admin_client.get("/api/v1/admin/stats").json()["catalogue"]
    assert catalogue["low_stock_threshold"] == 5


def test_products_nobody_can_buy_are_surfaced(admin_client: TestClient, db_session):
    """A product with no variant is filtered out of the public listing, so it is
    present in the admin console, absent from the shop, and nothing says so."""
    db_session.add(Product(cat_id="GHOST-1", name="Unbuyable", description="No variants"))
    make_product_with_variant(db_session, "REAL-1", stock=10)
    db_session.commit()

    catalogue = admin_client.get("/api/v1/admin/stats").json()["catalogue"]

    names = [p["name"] for p in catalogue["invisible_products"]]
    assert names == ["Unbuyable"]


def test_out_of_stock_is_counted_separately(admin_client: TestClient, db_session):
    make_product_with_variant(db_session, "GONE-1", stock=0)
    make_product_with_variant(db_session, "LOW-2", stock=3)

    catalogue = admin_client.get("/api/v1/admin/stats").json()["catalogue"]

    assert catalogue["out_of_stock"] == 1
    assert catalogue["low_stock_total"] == 2, "zero is also below the threshold"


# --- volume ------------------------------------------------------------------

def test_volume_counts_orders_per_window(admin_client: TestClient, db_session):
    recent, _ = make_order(db_session)
    old, _ = make_order(db_session)
    old.created_at = datetime.now(tz=timezone.utc) - timedelta(days=45)
    db_session.commit()

    volume = admin_client.get("/api/v1/admin/stats").json()["volume"]

    assert volume["all_time"] == 2
    assert volume["last_30_days"] == 1, "a 45-day-old order counted in the last 30"


def test_top_products_rank_by_units_sold(admin_client: TestClient, db_session):
    from app.models.order import OrderItem

    order, _ = make_order(db_session)
    order.items.append(
        OrderItem(
            variant_id=None,
            product_name="Anti-Tau",
            variant_label="50 ug",
            unit_price=Decimal("10.00"),
            quantity=7,
        )
    )
    db_session.commit()

    top = admin_client.get("/api/v1/admin/stats").json()["top_products"]

    assert top[0]["product_name"] == "Anti-Tau"
    assert top[0]["units"] == 7


def test_top_products_survive_the_product_being_deleted(admin_client: TestClient, db_session):
    """OrderItem carries the name denormalised, which is the whole point of
    historical reporting: last month's figures should not change because a
    product was removed today."""
    from app.models.order import OrderItem

    order, variant = make_order(db_session)
    order.items.append(
        OrderItem(
            variant_id=None,  # as ON DELETE SET NULL would leave it
            product_name="Discontinued Reagent",
            variant_label="10 ml",
            unit_price=Decimal("5.00"),
            quantity=3,
        )
    )
    db_session.commit()

    top = admin_client.get("/api/v1/admin/stats").json()["top_products"]

    assert any(p["product_name"] == "Discontinued Reagent" for p in top)


# --- access ------------------------------------------------------------------

def test_only_an_admin_sees_the_dashboard(client: TestClient):
    assert client.get("/api/v1/admin/stats").status_code in (401, 403)


def test_a_signed_in_customer_cannot_see_it(user_client: TestClient):
    assert user_client.get("/api/v1/admin/stats").status_code in (401, 403)


# --- the growth chart's data -------------------------------------------------

def test_the_series_covers_every_day_including_the_empty_ones(
    admin_client: TestClient, db_session
):
    """A chart drawn from only the days that had orders compresses time and
    makes a quiet fortnight look like steady trade."""
    make_order(db_session)

    daily = admin_client.get("/api/v1/admin/stats").json()["daily"]

    assert len(daily) == 30, f"expected 30 days, got {len(daily)}"
    assert sum(1 for d in daily if d["orders"] == 0) >= 28


def test_the_running_total_only_ever_climbs(admin_client: TestClient, db_session):
    for _ in range(3):
        make_order(db_session)

    daily = admin_client.get("/api/v1/admin/stats").json()["daily"]

    values = [d["cumulative"] for d in daily]
    assert values == sorted(values), "the growth line went backwards"
    assert values[-1] == 3


def test_the_curve_starts_where_the_business_is_not_at_zero(
    admin_client: TestClient, db_session
):
    """The point of a growth line is where the shop has got to. One that
    restarts at zero every month shows nothing."""
    old, _ = make_order(db_session)
    old.created_at = datetime.now(tz=timezone.utc) - timedelta(days=90)
    make_order(db_session)  # inside the window
    db_session.commit()

    daily = admin_client.get("/api/v1/admin/stats").json()["daily"]

    assert daily[0]["cumulative"] >= 1, "the 90-day-old order was forgotten"
    assert daily[-1]["cumulative"] == 2


def test_a_cancelled_order_is_absent_from_the_curve(admin_client: TestClient, db_session):
    make_order(db_session, status=OrderStatus.cancelled)

    daily = admin_client.get("/api/v1/admin/stats").json()["daily"]

    assert daily[-1]["cumulative"] == 0


def test_days_are_bucketed_in_the_business_zone(admin_client: TestClient, db_session):
    """An order at 9pm New York is already tomorrow in UTC. Bucketed naively it
    lands on the wrong bar, and by one day every evening."""
    zone = ZoneInfo("America/New_York")
    order, _ = make_order(db_session)
    local_evening = datetime.now(tz=zone).replace(hour=21, minute=0, second=0, microsecond=0)
    order.created_at = local_evening.astimezone(timezone.utc)
    db_session.commit()

    daily = admin_client.get("/api/v1/admin/stats").json()["daily"]
    expected = local_evening.date().isoformat()

    landed = [d["date"] for d in daily if d["orders"] > 0]
    assert landed == [expected], f"the evening order landed on {landed}, wanted {expected}"


# --- what the queue is worth --------------------------------------------------

def test_the_queue_is_priced_so_an_admin_can_judge_the_afternoon(
    admin_client: TestClient, db_session
):
    make_order(db_session, status=OrderStatus.awaiting_fulfillment)  # 10.00
    make_order(db_session, status=OrderStatus.confirmed)             # 10.00
    make_order(db_session, status=OrderStatus.shipped)               # already gone

    queue = admin_client.get("/api/v1/admin/stats").json()["queue"]

    assert queue["queue_value"] == 20.0, "shipped work is not still waiting"


def test_the_queue_value_excludes_tax(admin_client: TestClient, db_session):
    """Sales tax is not the shop's, and this is not a payout figure - Stripe is
    authoritative for money."""
    order, _ = make_order(db_session, status=OrderStatus.awaiting_fulfillment)
    order.tax_amount = Decimal("99.00")
    db_session.commit()

    queue = admin_client.get("/api/v1/admin/stats").json()["queue"]

    assert queue["queue_value"] == 10.0


def test_an_empty_queue_is_worth_nothing(admin_client: TestClient, db_session):
    assert admin_client.get("/api/v1/admin/stats").json()["queue"]["queue_value"] == 0


def test_the_queue_value_is_a_json_number_not_a_string(admin_client: TestClient, db_session):
    """Money crosses the API as a number. Bare Decimal serialises as a string and
    the frontend's type says number, which is the rule the Money alias exists
    to enforce."""
    import json as _json

    make_order(db_session, status=OrderStatus.awaiting_fulfillment)

    raw = _json.loads(admin_client.get("/api/v1/admin/stats").text)
    assert isinstance(raw["queue"]["queue_value"], (int, float)), raw["queue"]["queue_value"]


def test_the_reported_timezone_is_the_one_actually_used(admin_client: TestClient, monkeypatch):
    """A bad setting falls back to UTC for bucketing. Reporting the raw setting
    would have the footer claim a zone the bars are not in."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "business_timezone", "America/New_Yrok", raising=False)

    body = admin_client.get("/api/v1/admin/stats").json()

    assert body["timezone"] == "UTC", "the label named a zone the data is not bucketed in"


def test_an_order_dated_tomorrow_does_not_vanish_from_the_total(
    admin_client: TestClient, db_session
):
    """It used to be counted into the buckets and then dropped by the loop, so
    the curve's end silently disagreed with the all-time tile above it."""
    order, _ = make_order(db_session)
    order.created_at = datetime.now(tz=timezone.utc) + timedelta(days=2)
    db_session.commit()

    body = admin_client.get("/api/v1/admin/stats").json()

    assert body["daily"][-1]["cumulative"] == body["volume"]["all_time"]
