import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.services.order_numbers import generate_order_number


def make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment, qty=1, stock=5,
                         captured=None):
    """An order as the application would leave it.

    `captured` defaults to whatever the status implies: money is taken at
    confirm, so anything at confirmed or later has been captured. Pass it
    explicitly to build an order from before the capture moved - those carry no
    captured_at, and the code branches on the fact rather than the status
    precisely so they still behave correctly.
    """
    product = Product(cat_id=f"ADM-{uuid.uuid4().hex[:4]}", name="Admin Test", description="Test")
    variant = ProductVariant(
        catalog_id=f"ADM-V-{uuid.uuid4().hex[:4]}",
        size_value=Decimal("100"),
        size_unit="g",
        price=Decimal("20.00"),
        stock=stock,
    )
    product.variants.append(variant)
    db_session.add(product)
    # Explicit, because the OrderItem below needs variant.id. This used to work
    # only because the order_number expression ran a query, whose autoflush
    # assigned the id as a side effect - so changing an unrelated line silently
    # left variant_id as None and the stock assertions stopped meaning anything.
    db_session.flush()

    order = Order(
        order_number=generate_order_number(),
        user_id="customer-sub-abc",
        status=status,
        stripe_payment_intent_id=f"pi_{uuid.uuid4().hex}",
        shipping_name="Customer Name",
        shipping_phone="5559876543",
        shipping_address1="456 Elm St",
        shipping_city="Portland",
        shipping_state="OR",
        shipping_zip="97201",
        total_amount=Decimal("20.00"),
    )
    captured_now = captured
    if captured_now is None:
        captured_now = status in (
            OrderStatus.confirmed,
            OrderStatus.shipped,
            OrderStatus.delivered,
        )
    if captured_now:
        order.captured_at = datetime.now(tz=timezone.utc)

    item = OrderItem(
        variant_id=variant.id,
        product_name="Admin Test",
        variant_label="100g",
        unit_price=Decimal("20.00"),
        quantity=qty,
    )
    order.items.append(item)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    db_session.refresh(variant)
    return order, variant


def test_admin_list_orders(admin_client: TestClient, db_session):
    make_order_for_admin(db_session)
    make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.get("/api/v1/admin/orders")
    assert response.status_code == 200
    assert len(response.json()["items"]) >= 2


def test_admin_list_orders_status_filter(admin_client: TestClient, db_session):
    make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.get("/api/v1/admin/orders?status=shipped")
    assert response.status_code == 200
    data = response.json()["items"]
    assert all(o["status"] == "shipped" for o in data)


def test_admin_get_order_detail_includes_stock(admin_client: TestClient, db_session):
    order, variant = make_order_for_admin(db_session, qty=2, stock=3)

    response = admin_client.get(f"/api/v1/admin/orders/{order.id}")
    assert response.status_code == 200
    data = response.json()
    item = data["items"][0]
    assert item["current_stock"] == 3
    assert item["quantity"] == 2


def test_admin_confirm_order_captures_the_payment(admin_client: TestClient, db_session):
    """Confirming takes the money.

    This asserted the opposite while capture happened at ship, and that was the
    wrong step to hang it on: a card hold lapses after about a week, and packing
    cold-chain goods and waiting for a courier can outrun that. Confirming is a
    click, comfortably inside the window, and it is already where the admin
    commits - so taking the money there means shipping is no longer on a clock.
    """
    order, _ = make_order_for_admin(db_session)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent") as mock_capture:
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    mock_capture.assert_called_once_with(order.stripe_payment_intent_id)


def test_a_failed_capture_leaves_the_order_unconfirmed(admin_client: TestClient, db_session):
    """Captured before the status moves, deliberately. A confirmed order whose
    capture failed would sit with its stock deducted and no money behind it."""
    order, _ = make_order_for_admin(db_session)

    with patch(
        "app.api.v1.endpoints.admin_orders.capture_payment_intent",
        side_effect=Exception("card declined"),
    ):
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )

    assert response.status_code == 502
    db_session.refresh(order)
    assert order.status == OrderStatus.awaiting_fulfillment


def test_admin_confirm_order_leaves_stock_alone(admin_client: TestClient, db_session):
    """Confirming does not move stock — it was taken when the order was created.

    This asserted the opposite until stock moved to creation. Confirming is now
    only an acknowledgement, and the inventory was already set aside.
    """
    order, variant = make_order_for_admin(db_session, qty=2, stock=5)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent"):
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )
    assert response.status_code == 200
    db_session.refresh(variant)
    assert variant.stock == 5


def test_admin_confirm_order_cannot_fail_on_stock(admin_client: TestClient, db_session):
    """A variant at zero no longer blocks confirming an order that holds it.

    This is the admin-facing symptom the change exists to remove. The order's
    own stock was taken at creation, so a variant now reading zero means it has
    all been allocated - not that this order cannot be honoured. Confirming used
    to 400 here, stranding an order whose card was already authorised.
    """
    order, variant = make_order_for_admin(db_session, qty=3, stock=0)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent"):
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    db_session.refresh(variant)
    assert variant.stock == 0


def test_admin_ship_order_captures_no_stock_change(admin_client: TestClient, db_session):
    """Shipping no longer touches the money - it was captured at confirm - and
    leaves stock alone, which moved at creation."""
    order, variant = make_order_for_admin(db_session, status=OrderStatus.confirmed, qty=1, stock=4)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent") as mock_capture:
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "shipped"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "shipped"
    mock_capture.assert_not_called()

    db_session.refresh(variant)
    assert variant.stock == 4  # unchanged — taken at creation, not at ship


def test_admin_cancel_confirmed_restores_stock(admin_client: TestClient, db_session):
    """Cancelling a confirmed order restores stock."""
    order, variant = make_order_for_admin(db_session, status=OrderStatus.confirmed, qty=2, stock=3)

    # Refund, not void: confirming captured the money.
    with patch("app.services.stripe_service.create_refund"):
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    db_session.refresh(variant)
    assert variant.stock == 5  # 3 + 2 restored


def test_admin_cancel_pending_cancels_auth(admin_client: TestClient, db_session):
    """Cancelling a pending order (abandoned checkout) cancels the PI — no refund."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.pending)

    with patch("app.services.stripe_service.cancel_payment_intent") as mock_cancel, \
         patch("app.services.stripe_service.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_cancel.assert_called_once_with(order.stripe_payment_intent_id)
    mock_refund.assert_not_called()


def test_admin_cancel_awaiting_cancels_auth(admin_client: TestClient, db_session):
    """Cancelling an awaiting_fulfillment order cancels the auth — no refund."""
    order, _ = make_order_for_admin(db_session)

    with patch("app.services.stripe_service.cancel_payment_intent") as mock_cancel, \
         patch("app.services.stripe_service.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_cancel.assert_called_once_with(order.stripe_payment_intent_id)
    mock_refund.assert_not_called()


def test_admin_cancel_confirmed_issues_a_refund(admin_client: TestClient, db_session):
    """A confirmed order has been charged, so cancelling refunds rather than voids.

    The boundary moved with the capture. Voiding here would leave the customer
    paid-up with nothing coming - a void does not return money already taken.
    """
    order, _ = make_order_for_admin(db_session, status=OrderStatus.confirmed)

    with patch("app.services.stripe_service.cancel_payment_intent") as mock_cancel,\
         patch("app.services.stripe_service.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_refund.assert_called_once_with(order.stripe_payment_intent_id)
    mock_cancel.assert_not_called()


def test_admin_cancel_shipped_issues_refund(admin_client: TestClient, db_session):
    """Cancelling a shipped order issues a refund — money was already captured."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped)

    with patch("app.services.stripe_service.create_refund") as mock_refund, \
         patch("app.services.stripe_service.cancel_payment_intent") as mock_cancel:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_refund.assert_called_once_with(order.stripe_payment_intent_id)
    mock_cancel.assert_not_called()


def test_admin_cancel_delivered_issues_refund(admin_client: TestClient, db_session):
    """Cancelling a delivered order issues a refund - the money is long since
    taken, whether at confirm (now) or at ship (before the capture moved)."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.delivered)

    with patch("app.services.stripe_service.create_refund") as mock_refund, \
         patch("app.services.stripe_service.cancel_payment_intent") as mock_cancel:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    mock_refund.assert_called_once_with(order.stripe_payment_intent_id)
    mock_cancel.assert_not_called()


def test_admin_ship_wrong_status_returns_400(admin_client: TestClient, db_session):
    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.patch(
        f"/api/v1/admin/orders/{order.id}/status",
        json={"status": "shipped"},
    )
    assert response.status_code == 400


def test_admin_cancel_already_cancelled_returns_400(admin_client: TestClient, db_session):
    """Cancel on already-cancelled order must not call any Stripe operation."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.cancelled)

    with patch("app.services.stripe_service.cancel_payment_intent") as mock_cancel, \
         patch("app.services.stripe_service.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 400
    mock_cancel.assert_not_called()
    mock_refund.assert_not_called()


def test_admin_deliver_order(admin_client: TestClient, db_session):
    """PATCH status=delivered transitions shipped → delivered."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped)

    response = admin_client.patch(
        f"/api/v1/admin/orders/{order.id}/status",
        json={"status": "delivered"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"


def test_admin_ship_nonexistent_order_returns_404(admin_client: TestClient):
    response = admin_client.patch(
        f"/api/v1/admin/orders/{uuid.uuid4()}/status",
        json={"status": "shipped"},
    )
    assert response.status_code == 404


# --- the status query parameter ---

def test_status_filter_still_answers_to_the_status_query_parameter(admin_client, db_session):
    """The handler's parameter was renamed to status_filter because `status`
    shadowed the fastapi status module - which is why this one function had to
    hardcode 400 where every sibling uses the constant. The URL contract is
    unchanged, and this is what proves it."""
    from app.models.order import OrderStatus
    from app.tests.test_orders import make_order

    make_order(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order(db_session, status=OrderStatus.delivered)

    response = admin_client.get("/api/v1/admin/orders?status=delivered")

    assert response.status_code == 200
    assert [o["status"] for o in response.json()["items"]] == ["delivered"]


def test_an_unknown_status_is_a_400_from_the_shared_constant(admin_client):
    response = admin_client.get("/api/v1/admin/orders?status=not-a-status")

    assert response.status_code == 400
    assert "not-a-status" in response.json()["detail"]


def test_no_status_returns_every_order(admin_client, db_session):
    from app.models.order import OrderStatus
    from app.tests.test_orders import make_order

    make_order(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order(db_session, status=OrderStatus.delivered)

    response = admin_client.get("/api/v1/admin/orders")

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 2


# --- paging and search ---

def test_a_page_reports_how_many_orders_there_are_in_total(admin_client, db_session):
    """A page is not useful on its own. Without the count the console cannot say
    "50 of 340", and cannot know whether a next page exists."""
    for _ in range(3):
        make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    response = admin_client.get("/api/v1/admin/orders?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0


def test_paging_walks_the_whole_list_without_repeating_an_order(admin_client, db_session):
    for _ in range(5):
        make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    first = admin_client.get("/api/v1/admin/orders?limit=2&offset=0").json()["items"]
    second = admin_client.get("/api/v1/admin/orders?limit=2&offset=2").json()["items"]
    third = admin_client.get("/api/v1/admin/orders?limit=2&offset=4").json()["items"]

    seen = [o["id"] for o in first + second + third]
    assert len(seen) == 5
    assert len(set(seen)) == 5, "an order appeared on two pages"


def test_paging_orders_by_something_unique():
    """The sort key must be a total order, not just created_at.

    OFFSET does not remember which rows the previous page returned; it re-runs
    the query and skips N. If two orders sort equal, the database may order them
    differently between two requests, and one order then appears on two pages
    while another is never shown at all. Ties are not hypothetical - SQLite's
    CURRENT_TIMESTAMP has one-second granularity, and a burst of checkouts
    collides.

    Asserted against the compiled statement rather than by paging real rows,
    because SQLite happens to return insertion order for a tie and so passes a
    behavioural version of this test with the tiebreak deleted. A test that
    cannot fail when the guarantee is removed is not a test; on the database
    this actually runs against, an unstable sort is free to reorder.
    """
    from app.services.order_service import ORDER_LISTING_SORT

    ordering = [str(clause) for clause in ORDER_LISTING_SORT]

    assert any("created_at" in clause for clause in ordering)
    assert any("orders.id" in clause for clause in ordering), (
        "ordering has no unique tiebreak, so OFFSET paging can repeat or skip rows"
    )


def test_search_finds_an_order_by_its_number(admin_client, db_session):
    """Search runs on the server now. It used to be a client filter over every
    order ever fetched, which only worked because the list was unpaginated - the
    moment a page is a page, that filter searches one page and silently reports
    nothing for the rest."""
    target, _ = make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    response = admin_client.get(f"/api/v1/admin/orders?q={target.order_number}")

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["order_number"] == target.order_number


def test_search_matches_a_partial_email_case_insensitively(admin_client, db_session):
    order, _ = make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    order.customer_email = "Researcher@Example.COM"
    # A second order that must NOT match. Without it this asserts "the only
    # order in the database was returned", which is true whether or not the
    # search filter exists - mutation-testing caught exactly that.
    other, _ = make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    other.customer_email = "someone-else@example.com"
    db_session.commit()

    response = admin_client.get("/api/v1/admin/orders?q=researcher@ex")

    assert response.json()["total"] == 1
    assert response.json()["items"][0]["customer_email"] == "Researcher@Example.COM"


def test_search_and_status_narrow_together(admin_client, db_session):
    """Both filters at once, or the tabs and the search box fight each other."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped)
    # A second shipped order, so "status=shipped" alone would return two. Without
    # it the status filter satisfies the assertion on its own and the search is
    # never exercised.
    make_order_for_admin(db_session, status=OrderStatus.shipped)
    make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    hit = admin_client.get(f"/api/v1/admin/orders?status=shipped&q={order.order_number}")
    miss = admin_client.get(f"/api/v1/admin/orders?status=delivered&q={order.order_number}")

    assert hit.json()["total"] == 1
    assert miss.json()["total"] == 0


def test_an_absurd_page_size_is_refused_at_the_boundary(admin_client, db_session):
    """limit is a query parameter, so it is caller-controlled. Unbounded, it is
    a way to ask for every order in one request.

    This is FastAPI's `le=` doing the work, not the service - the request never
    reaches it. The service clamps as well, for callers that bypass the
    endpoint; that is asserted separately below.
    """
    make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    response = admin_client.get("/api/v1/admin/orders?limit=100000")

    assert response.status_code == 422


def test_the_service_clamps_a_page_size_the_endpoint_would_have_rejected(
    db_session, monkeypatch
):
    """Called directly, with no FastAPI validation in front of it.

    The maximum is lowered for the test rather than creating two hundred orders
    to exceed the real one. Asserting `len(orders) <= 200` with three orders in
    the database passes whether or not the clamp exists - mutation-testing
    caught that, twice.
    """
    from app.services import order_service

    monkeypatch.setattr(order_service, "MAX_ORDER_PAGE_SIZE", 2)
    for _ in range(3):
        make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    orders, total = order_service.list_all_orders(db_session, limit=100_000)

    assert total == 3, "the count is of every match, not of the page"
    assert len(orders) == 2, "the page size was not clamped"


def test_a_page_size_below_one_is_raised_to_one(db_session):
    """limit=0 would otherwise emit LIMIT 0 and return nothing forever."""
    from app.services.order_service import list_all_orders

    make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    orders, total = list_all_orders(db_session, limit=0)

    assert total == 1
    assert len(orders) == 1


def test_search_over_no_matches_is_an_empty_page_not_an_error(admin_client, db_session):
    make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    response = admin_client.get("/api/v1/admin/orders?q=nothing-matches-this")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


# --- orders that straddle the capture move ------------------------------------
#
# Capture used to be inferred from status, and that inference inverted when the
# capture moved from ship to confirm. An order confirmed under the old rule
# carries no captured_at, and the code branches on the fact rather than the
# status precisely so it still behaves correctly - no backfill that would have
# to guess.

def test_an_order_confirmed_before_the_change_is_captured_when_it_ships(
    admin_client: TestClient, db_session
):
    """Otherwise it ships and the authorisation lapses uncaptured - the goods
    leave and the merchant is never paid."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.confirmed, captured=False)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent") as mock_capture:
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "shipped"},
        )

    assert response.status_code == 200
    mock_capture.assert_called_once_with(order.stripe_payment_intent_id)
    db_session.refresh(order)
    assert order.captured_at is not None


def test_an_order_confirmed_before_the_change_is_voided_not_refunded(
    admin_client: TestClient, db_session
):
    """Stripe rejects a refund on an uncaptured intent, so branching on status
    made such an order impossible to cancel at all."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.confirmed, captured=False)

    with patch("app.services.stripe_service.cancel_payment_intent") as mock_cancel, \
         patch("app.services.stripe_service.create_refund") as mock_refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    mock_cancel.assert_called_once_with(order.stripe_payment_intent_id)
    mock_refund.assert_not_called()


def test_confirming_twice_does_not_capture_twice(admin_client: TestClient, db_session):
    """A retry after a failed commit would otherwise capture again, which Stripe
    rejects - a permanent 502 on an order that could never be confirmed."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    order.captured_at = datetime.now(tz=timezone.utc)
    db_session.commit()

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent") as mock_capture:
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )

    assert response.status_code == 200
    mock_capture.assert_not_called()


def test_confirming_records_that_the_money_moved(admin_client: TestClient, db_session):
    order, _ = make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    assert order.captured_at is None

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent"):
        admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )

    db_session.refresh(order)
    assert order.captured_at is not None, "nothing recorded that the card was charged"


def test_a_retry_after_a_lost_record_does_not_charge_again(
    admin_client: TestClient, db_session
):
    """The capture and the status change are one transaction, so a rollback
    after a successful capture erases captured_at while the money has gone.

    Committing the record separately fixed that and broke something worse: it
    ended the transaction, releasing the row lock between the capture and the
    status change, so a cancel landing in the gap refunded and restocked an
    order that then became confirmed.

    So the transaction stays whole, and the retry is safe because Stripe refuses
    to capture twice - handled inside capture_payment_intent rather than
    pre-empted by a retrieve before every confirm. See
    test_capturing_an_already_captured_intent_is_not_an_error for the guarantee
    this leans on.
    """
    order, _ = make_order_for_admin(db_session)

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent") as mock_capture:
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )

    assert response.status_code == 200
    mock_capture.assert_called_once_with(order.stripe_payment_intent_id)
    db_session.refresh(order)
    assert order.captured_at is not None, "the retry must record what it found"



def test_the_capture_and_the_status_change_are_one_transaction(
    admin_client: TestClient, db_session
):
    """Nothing may commit between them.

    An intermediate commit released the row lock, and a cancel taken in that gap
    left the order confirmed, refunded and restocked at once.
    """
    order, _ = make_order_for_admin(db_session)
    commits = []
    real_commit = db_session.commit

    def counting_commit():
        commits.append(1)
        return real_commit()

    with patch("app.api.v1.endpoints.admin_orders.capture_payment_intent"), \
         patch.object(db_session, "commit", counting_commit):
        response = admin_client.patch(
            f"/api/v1/admin/orders/{order.id}/status",
            json={"status": "confirmed"},
        )

    assert response.status_code == 200
    assert len(commits) == 1, (
        f"{len(commits)} commits during confirm - anything above one drops the "
        "row lock between the capture and the status change"
    )


def test_a_legacy_shipped_order_is_refunded_not_voided(admin_client: TestClient, db_session):
    """The migration leaves captured_at NULL on every pre-existing row.

    That includes orders shipped under the old rule, where capture happened at
    ship and the money really did move. Trusting the local record alone voids a
    captured intent, Stripe rejects it, and the customer is never refunded - the
    same dead end the column was added to remove, with the arms swapped.
    """
    from app.services.stripe_service import PaymentAlreadyCaptured

    order, _ = make_order_for_admin(db_session, status=OrderStatus.shipped, captured=False)
    assert order.captured_at is None

    # Stripe is what corrects us now: the void is attempted, rejected because
    # the intent already succeeded, and the refund follows. No up-front
    # consult, so an ordinary cancellation pays nothing for this.
    with patch(
        "app.services.stripe_service.cancel_payment_intent",
        side_effect=PaymentAlreadyCaptured("pi"),
    ) as void, patch("app.services.stripe_service.create_refund") as refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    void.assert_called_once()
    refund.assert_called_once_with(order.stripe_payment_intent_id)


def test_an_uncaptured_order_is_still_voided_not_refunded(admin_client: TestClient, db_session):
    """A void is free and a refund is not, so asking Stripe must not turn every
    cancellation into a fee."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)

    with patch("app.services.stripe_service.cancel_payment_intent") as void, \
         patch("app.services.stripe_service.create_refund") as refund:
        response = admin_client.post(f"/api/v1/admin/orders/{order.id}/cancel")

    assert response.status_code == 200
    void.assert_called_once_with(order.stripe_payment_intent_id)
    refund.assert_not_called()


def test_captured_at_survives_model_validate(db_session):
    """The schema must match the column, not the builder that feeds it.

    As Optional[str] this passed only because _build_admin_order_out called
    .isoformat() by hand. AdminOrderOut sets from_attributes=True, so validating
    an ORM object directly - the obvious next step, and what returning a bare
    Order under this response_model does - raised ValidationError and 500'd.
    """
    from datetime import datetime, timezone

    from app.schemas.order import AdminOrderOut

    order, _ = make_order_for_admin(db_session, status=OrderStatus.confirmed)
    order.captured_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    db_session.commit()

    out = AdminOrderOut.model_validate(order)

    assert out.captured_at == order.captured_at


def test_the_admin_api_reports_the_shipping_it_charged(admin_client: TestClient, db_session):
    """Every field the confirm dialog sums has to survive the response.

    _build_admin_order_out constructs AdminOrderOut by hand, and the schema
    defaults shipping_amount to 0 - so omitting it reported no shipping on every
    order rather than failing. The console then quoted a total short by the fee,
    on the dialog whose entire purpose is stating what is about to be charged,
    while the customer's own page showed the real figure.
    """
    order, _ = make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    order.shipping_amount = Decimal("9.99")
    db_session.commit()

    body = admin_client.get(f"/api/v1/admin/orders/{order.id}").json()

    assert body["shipping_amount"] == 9.99
    assert body["tax_amount"] == float(order.tax_amount)
    assert body["total_amount"] == float(order.total_amount)


def test_the_shipping_fee_survives_the_list_as_well(admin_client: TestClient, db_session):
    """The list uses the same builder, and the orders table renders totals."""
    order, _ = make_order_for_admin(db_session, status=OrderStatus.awaiting_fulfillment)
    order.shipping_amount = Decimal("9.99")
    db_session.commit()

    body = admin_client.get("/api/v1/admin/orders?status=awaiting_fulfillment").json()

    assert body["items"][0]["shipping_amount"] == 9.99

