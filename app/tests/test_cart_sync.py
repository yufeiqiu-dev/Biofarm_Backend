"""Reconciling a device's local basket with the server's.

The browser is local-first now: it edits localStorage and only talks to the
server at a handful of sync points (idle, tab hide, sign-in, sign-out - see
Biofarm_KnowledgeBase/documentation/designs/2026-09-08-local-first-cart-sync.md).
Each of those pushes the *whole* local basket, so this is where the interesting
behaviour lives - not the per-line endpoints, which are a single device acting
right now and always win.
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.tests.test_cart import USER, make_variant

NOW = datetime.now(tz=timezone.utc)
EARLIER = NOW - timedelta(hours=2)
# Within the clock-skew grace, so it is clamped rather than skipped.
SLIGHTLY_LATER = NOW + timedelta(seconds=90)
FAR_FUTURE = NOW + timedelta(days=30)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _sync(user_client: TestClient, items: list[dict]):
    return user_client.put("/api/v1/cart", json={"items": items})


def test_a_new_line_is_accepted(user_client: TestClient, db_session):
    variant = make_variant(db_session, "SYN-01", price="10.00", stock=5)

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 2, "client_updated_at": _iso(NOW)}],
    ).json()

    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 2


def test_a_newer_local_edit_overwrites_the_server(user_client: TestClient, db_session):
    """The whole point. A device that was offline still gets its later edit
    applied when it finally syncs."""
    variant = make_variant(db_session, "SYN-02", stock=5)
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}])

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 4, "client_updated_at": _iso(NOW)}],
    ).json()

    assert body["items"][0]["quantity"] == 4


def test_an_older_incoming_line_does_not_overwrite_the_server(user_client: TestClient, db_session):
    """A device syncing a stale snapshot - it went offline before the last edit
    landed elsewhere - must not undo what has already happened."""
    variant = make_variant(db_session, "SYN-03", stock=5)
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 4, "client_updated_at": _iso(NOW)}])

    # A device whose local copy predates that edit, syncing after the fact.
    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}],
    ).json()

    assert body["items"][0]["quantity"] == 4, "a stale sync overwrote a newer server line"


def test_a_tie_changes_nothing(user_client: TestClient, db_session):
    # Equal timestamps keep the server's own value rather than adopting the
    # incoming one - otherwise two devices whose clocks happen to agree would
    # keep re-writing the same line to each other forever.
    variant = make_variant(db_session, "SYN-04", stock=5)
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 3, "client_updated_at": _iso(NOW)}])

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 9, "client_updated_at": _iso(NOW)}],
    ).json()

    assert body["items"][0]["quantity"] == 3


def test_a_newer_deletion_removes_a_live_line(user_client: TestClient, db_session):
    variant = make_variant(db_session, "SYN-05", stock=5)
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 2, "client_updated_at": _iso(EARLIER)}])

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 2, "client_updated_at": _iso(NOW), "deleted": True}],
    ).json()

    assert body["items"] == []


def test_a_stale_line_does_not_resurrect_a_newer_deletion(user_client: TestClient, db_session):
    """The scenario the whole tombstone design exists for: a phone offline in a
    subway tunnel syncs a line the laptop already deleted."""
    variant = make_variant(db_session, "SYN-06", stock=5)
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}])

    # The laptop deletes it.
    _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(NOW), "deleted": True}],
    )

    # The phone, still holding its pre-deletion copy from before it went
    # offline, finally reconnects and syncs.
    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}],
    ).json()

    assert body["items"] == [], "a stale device brought a deleted item back"


def test_a_newer_edit_revives_a_tombstone(user_client: TestClient, db_session):
    """The other direction: the customer deletes something, then changes their
    mind and re-adds it. The later action wins, whichever it is."""
    variant = make_variant(db_session, "SYN-07", stock=5)
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}])
    _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER + timedelta(minutes=1)), "deleted": True}],
    )

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 3, "client_updated_at": _iso(SLIGHTLY_LATER)}],
    ).json()

    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 3


def test_a_deletion_with_no_server_row_is_a_no_op(user_client: TestClient, db_session):
    # A device tombstoning a line the server never had - it was added and
    # removed again before this device ever synced. Nothing to overwrite.
    variant = make_variant(db_session, "SYN-08", stock=5)

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(NOW), "deleted": True}],
    ).json()

    assert body["items"] == []


def test_lines_the_push_does_not_mention_are_left_alone(user_client: TestClient, db_session):
    """How an item added by a different device survives a push from a device
    that never learned about it. This is the whole reason merge is per-line and
    not a whole-basket replace."""
    a = make_variant(db_session, "SYN-09-A", stock=5)
    b = make_variant(db_session, "SYN-09-B", stock=5)

    # One device adds A.
    _sync(user_client, [{"variant_id": str(a.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}])
    # A second device, whose snapshot predates A, adds B and syncs - mentioning
    # only B, because it does not know A exists.
    body = _sync(
        user_client,
        [{"variant_id": str(b.id), "quantity": 1, "client_updated_at": _iso(NOW)}],
    ).json()

    catalog_numbers = {item["catalog_number"] for item in body["items"]}
    assert catalog_numbers == {"SYN-09-A", "SYN-09-B"}, "the other device's line was lost"


def test_two_devices_editing_different_lines_both_survive(user_client: TestClient, db_session):
    a = make_variant(db_session, "SYN-10-A", stock=5)
    b = make_variant(db_session, "SYN-10-B", stock=5)
    _sync(
        user_client,
        [
            {"variant_id": str(a.id), "quantity": 1, "client_updated_at": _iso(EARLIER)},
            {"variant_id": str(b.id), "quantity": 1, "client_updated_at": _iso(EARLIER)},
        ],
    )

    # Device 1 raises A. Device 2, syncing separately, raises B. Neither
    # mentions the other's line.
    _sync(user_client, [{"variant_id": str(a.id), "quantity": 5, "client_updated_at": _iso(NOW)}])
    body = _sync(user_client, [{"variant_id": str(b.id), "quantity": 7, "client_updated_at": _iso(NOW)}]).json()

    by_variant = {item["catalog_number"]: item["quantity"] for item in body["items"]}
    assert by_variant == {"SYN-10-A": 5, "SYN-10-B": 7}


def test_a_slightly_future_clock_is_clamped_not_skipped(user_client: TestClient, db_session):
    """A clock a little fast is ordinary. Its edit is clamped to server time and
    still applied - not dropped."""
    variant = make_variant(db_session, "SYN-11", stock=5)
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}])

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 9, "client_updated_at": _iso(SLIGHTLY_LATER)}],
    ).json()

    assert body["items"][0]["quantity"] == 9


def test_a_far_future_clock_is_skipped(user_client: TestClient, db_session):
    """Clamping a badly-skewed clock does not help: a fire-and-forget client
    re-sends the same future stamp forever, the clamp target keeps advancing,
    and the line drifts past every real edit - and once server time passes the
    stamp, resurrects purchases. So it is dropped, like an unknown variant."""
    variant = make_variant(db_session, "SYN-11b", stock=5)
    # A genuine earlier edit from another device.
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 4, "client_updated_at": _iso(EARLIER)}])

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 99, "client_updated_at": _iso(FAR_FUTURE)}],
    ).json()

    # The skewed device's line was ignored; the real edit stands.
    assert body["items"][0]["quantity"] == 4


def test_the_clamped_timestamp_is_reported_back(user_client: TestClient, db_session):
    # The client is expected to adopt whatever the server actually accepted,
    # not blindly keep re-sending a clock-skewed value.
    variant = make_variant(db_session, "SYN-12", stock=5)

    response = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(SLIGHTLY_LATER)}],
    )

    accepted = datetime.fromisoformat(response.json()["items"][0]["client_updated_at"])
    assert accepted < SLIGHTLY_LATER


def test_a_variant_that_no_longer_exists_is_skipped_not_rejected(user_client: TestClient):
    """A device's local basket referencing a discontinued product must not fail
    the whole sync - the rest of the basket still has to go through."""
    import uuid as _uuid

    response = _sync(
        user_client,
        [{"variant_id": str(_uuid.uuid4()), "quantity": 1, "client_updated_at": _iso(NOW)}],
    )

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_an_over_stock_line_is_reported(user_client: TestClient, db_session):
    variant = make_variant(db_session, "SYN-13", stock=2)

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 5, "client_updated_at": _iso(NOW)}],
    ).json()

    assert body["unavailable"] == ["SYN-13"]


def test_an_empty_push_is_a_no_op_not_a_clear(user_client: TestClient, db_session):
    """A device with nothing to report - e.g. the idle debounce firing with no
    pending edits - must not be interpreted as "the basket is empty"."""
    variant = make_variant(db_session, "SYN-14", stock=5)
    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(NOW)}])

    body = _sync(user_client, []).json()

    assert len(body["items"]) == 1


def test_sync_needs_a_signed_in_customer(client: TestClient):
    assert client.put("/api/v1/cart", json={"items": []}).status_code in (401, 403)


class TestConcurrentWritesAskForTheRowLock:
    """SQLite ignores FOR UPDATE, so the suite can only check that the service
    asks. scripts/check_cart_merge_race.py runs it against real Postgres -
    clear_bought_lines' scenario there fails every run without the lock.
    """

    def test_apply_merge_line_locks_the_row_it_compares(self):
        # Two overlapping merges must not both read the same `existing`, both
        # judge themselves newer, and both UPDATE - the older edit would then
        # win on commit order rather than on its clock.
        import inspect
        from app.services import cart_service

        src = inspect.getsource(cart_service._apply_merge_line)
        assert src.count(".with_for_update()") == 2, (
            "both the initial and the post-IntegrityError re-read must lock"
        )

    def test_clear_bought_lines_locks_before_the_decrement(self):
        import inspect
        from app.services import cart_service

        src = inspect.getsource(cart_service.clear_bought_lines)
        assert ".with_for_update()" in src, (
            "the quantity decrement is a read-modify-write; without the lock a "
            "concurrent webhook loses a decrement"
        )

    def test_clear_cart_locks_in_variant_id_order(self):
        # A bare bulk UPDATE would lock the owner's rows in scan order and could
        # deadlock a concurrent clear_bought_lines for the same customer.
        import inspect
        from app.services import cart_service

        src = inspect.getsource(cart_service.clear_cart)
        assert ".with_for_update()" in src and "order_by(CartItem.variant_id)" in src

    def test_set_line_locks_before_its_unconditional_overwrite(self):
        # set_line always wins whatever it touches, by design - which is
        # exactly why an unlocked read is dangerous: a stale snapshot plus an
        # unconditional `deleted_at=None` can resurrect a line a concurrent
        # clear_bought_lines just tombstoned. Both the initial read and the
        # post-IntegrityError re-read must lock.
        import inspect
        from app.services import cart_service

        src = inspect.getsource(cart_service.set_line)
        assert src.count(".with_for_update()") == 2


def test_one_line_that_fails_to_apply_does_not_fail_the_whole_sync(
    user_client: TestClient, db_session, monkeypatch
):
    """sync_cart has no try/except of its own; merge_cart isolates each line in
    a savepoint so a lock timeout, a deadlock, or a DataError on one line
    cannot 500 the push and lose every other line."""
    from app.services import cart_service

    good = make_variant(db_session, "SYN-SP-A", stock=5)
    bad = make_variant(db_session, "SYN-SP-B", stock=5)

    real = cart_service._apply_merge_line

    def flaky(db, owner, line, now):
        if line.variant_id == bad.id:
            raise RuntimeError("simulated per-line failure")
        return real(db, owner, line, now)

    monkeypatch.setattr(cart_service, "_apply_merge_line", flaky)

    response = _sync(
        user_client,
        [
            {"variant_id": str(good.id), "quantity": 2, "client_updated_at": _iso(NOW)},
            {"variant_id": str(bad.id), "quantity": 3, "client_updated_at": _iso(NOW)},
        ],
    )

    assert response.status_code == 200
    by_variant = {i["variant_id"]: i["quantity"] for i in response.json()["items"]}
    assert by_variant == {str(good.id): 2}, "the good line was lost with the bad one"


def test_one_customer_cannot_touch_another_basket_via_sync(user_client: TestClient, db_session):
    from app.services.cart_service import CartOwner, get_cart, set_line

    variant = make_variant(db_session, "SYN-15", stock=5)
    set_line(db_session, CartOwner.user("someone-else"), variant.id, 4)

    _sync(user_client, [{"variant_id": str(variant.id), "quantity": 9, "client_updated_at": _iso(NOW)}])

    others = get_cart(db_session, CartOwner.user("someone-else"))
    assert len(others) == 1 and others[0].quantity == 4, "one customer's sync affected another's basket"


def test_a_zero_or_negative_quantity_is_clamped_not_rejected(user_client: TestClient, db_session):
    # A background sync must not 422 over one line. No client sends quantity 0
    # for a live line (removal is deleted=True with the last quantity kept), so
    # this is corruption or an old client; _apply_merge_line floors it to 1
    # rather than failing the request or writing a value the CHECK refuses.
    variant = make_variant(db_session, "SYN-16", stock=5)

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 0, "client_updated_at": _iso(NOW)}],
    ).json()

    assert body["items"][0]["quantity"] == 1


def test_a_sync_payload_past_the_line_cap_is_rejected(user_client: TestClient, db_session):
    # merge_cart loops one locking SELECT per line in one transaction, so the
    # payload length is the ceiling on how long it holds row locks. A real
    # basket is nowhere near MAX_SYNC_LINES.
    from app.schemas.cart import MAX_SYNC_LINES

    variant = make_variant(db_session, "SYN-CAP", stock=5)
    items = [
        {"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(NOW)}
        for _ in range(MAX_SYNC_LINES + 1)
    ]

    assert _sync(user_client, items).status_code == 422


def test_a_non_integer_quantity_is_still_rejected(user_client: TestClient, db_session):
    # The line is drawn at "not an integer" - that is a structurally broken
    # request, not a stale value.
    variant = make_variant(db_session, "SYN-16d", stock=5)

    response = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": "lots", "client_updated_at": _iso(NOW)}],
    )

    assert response.status_code == 422


def test_an_over_max_quantity_is_clamped_not_rejected(user_client: TestClient, db_session):
    # A background sync of the whole basket must not 422 over one poisoned line
    # - every other bad-line case is skipped or clamped per line. set_line
    # clamps the same way.
    variant = make_variant(db_session, "SYN-16b", stock=5)

    body = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 100000, "client_updated_at": _iso(NOW)}],
    ).json()

    assert body["items"][0]["quantity"] == 999


def test_a_naive_client_timestamp_does_not_500_the_sync(user_client: TestClient, db_session):
    # The frontend sends toISOString() (always "Z"), but an old client or a
    # corrupted localStorage value can send a zoneless timestamp. That must
    # fail one line at worst, not the whole request with a TypeError.
    variant = make_variant(db_session, "SYN-16c", stock=5)

    response = _sync(
        user_client,
        [{"variant_id": str(variant.id), "quantity": 2, "client_updated_at": "2026-09-08T23:00:00"}],
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["quantity"] == 2


class TestSweepCartTombstones:
    def test_an_old_tombstone_is_removed(self, db_session):
        from app.services.cart_service import CartOwner, remove_line, set_line, sweep_cart_tombstones
        from app.models.cart_item import CartItem
        from sqlalchemy import select as _select

        variant = make_variant(db_session, "SYN-17", stock=5)
        owner = CartOwner.user(USER)
        set_line(db_session, owner, variant.id, 1)
        remove_line(db_session, owner, variant.id)

        # Backdated past the sweep's cutoff, as if it had been deleted 91 days
        # ago rather than a moment ago.
        row = db_session.scalars(_select(CartItem).where(CartItem.variant_id == variant.id)).first()
        row.deleted_at = datetime.now(tz=timezone.utc) - timedelta(days=91)
        db_session.commit()

        removed = sweep_cart_tombstones(db_session, max_age_days=90)

        assert removed == 1
        assert db_session.scalars(_select(CartItem)).first() is None

    def test_a_recent_tombstone_is_kept(self, db_session):
        """A device could plausibly still be offline holding a pre-deletion
        copy; sweeping too soon would let it resurrect what was deleted."""
        from app.services.cart_service import CartOwner, remove_line, set_line, sweep_cart_tombstones

        variant = make_variant(db_session, "SYN-18", stock=5)
        owner = CartOwner.user(USER)
        set_line(db_session, owner, variant.id, 1)
        remove_line(db_session, owner, variant.id)

        removed = sweep_cart_tombstones(db_session, max_age_days=90)

        assert removed == 0

    def test_a_live_line_is_never_swept(self, db_session):
        from app.services.cart_service import CartOwner, set_line, sweep_cart_tombstones
        from app.models.cart_item import CartItem
        from sqlalchemy import select as _select

        variant = make_variant(db_session, "SYN-19", stock=5)
        set_line(db_session, CartOwner.user(USER), variant.id, 1)
        row = db_session.scalars(_select(CartItem).where(CartItem.variant_id == variant.id)).first()
        # Old, but never deleted.
        row.created_at = datetime.now(tz=timezone.utc) - timedelta(days=365)
        db_session.commit()

        removed = sweep_cart_tombstones(db_session, max_age_days=90)

        assert removed == 0


class TestClearBoughtLinesTombstones:
    """clear_bought_lines has to tombstone, not hard-delete - a real delete
    would let a device that has been offline since before checkout resurrect
    exactly what was just paid for the next time it syncs."""

    def test_a_fully_bought_line_becomes_a_tombstone_not_a_gone_row(self, client, db_session):
        from app.models.cart_item import CartItem
        from app.services.cart_service import CartOwner, get_cart, set_line
        from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

        variant = make_variant(db_session, "SYN-20", stock=5)
        buyer = CartOwner.user("wh-user")
        set_line(db_session, buyer, variant.id, 1)
        make_checkout_session(db_session, "pi_tombstone_not_gone", variant.id)

        post_webhook(client, "pi_tombstone_not_gone", "payment_intent.amount_capturable_updated")

        row = db_session.query(CartItem).filter(CartItem.variant_id == variant.id).first()
        assert row is not None, "the row was hard-deleted rather than tombstoned"
        assert row.deleted_at is not None
        assert get_cart(db_session, buyer) == []

    def test_the_purchase_outweighs_a_stale_offline_device(self, client, db_session):
        """A device offline since before checkout, syncing afterwards with its
        old pre-purchase quantity, must not put the bought units back.

        Exercised at the service level, like the rest of this class: the point
        under test is the interaction between clear_bought_lines and
        merge_cart, not the HTTP layer, and going through it would mean forging
        a second identity's auth for no benefit.
        """
        from app.services.cart_service import CartOwner, CartLineMerge, get_cart, merge_cart, set_line
        from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

        variant = make_variant(db_session, "SYN-21", stock=5)
        buyer = CartOwner.user("wh-user")
        set_line(db_session, buyer, variant.id, 1)
        stale_snapshot = datetime.now(tz=timezone.utc)
        make_checkout_session(db_session, "pi_beats_stale_device", variant.id)

        post_webhook(client, "pi_beats_stale_device", "payment_intent.amount_capturable_updated")

        # The offline phone, syncing its pre-purchase snapshot after the fact.
        merge_cart(
            db_session,
            buyer,
            [CartLineMerge(variant_id=variant.id, quantity=1, client_updated_at=stale_snapshot)],
        )

        assert get_cart(db_session, buyer) == [], "a stale device brought back what was just bought"

    def test_a_line_already_tombstoned_by_the_customer_is_left_alone(self, client, db_session):
        """Checked out with X, removed X before the payment landed, then re-added
        X from another device. The purchase must not bump the clock past that
        re-add - the removal's own clock already guards against a stale
        resurrection.
        """
        from datetime import timedelta as _td

        from app.models.cart_item import CartItem
        from app.services.cart_service import (
            CartLineMerge,
            CartOwner,
            get_cart,
            merge_cart,
            remove_line,
            set_line,
        )
        from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

        variant = make_variant(db_session, "SYN-21b", stock=5)
        buyer = CartOwner.user("wh-user")
        set_line(db_session, buyer, variant.id, 1)
        make_checkout_session(db_session, "pi_tombstoned_left_alone", variant.id)

        # The customer removes X after checking out.
        remove_line(db_session, buyer, variant.id)
        row = db_session.query(CartItem).filter(CartItem.variant_id == variant.id).first()
        removed_at = row.client_updated_at

        post_webhook(client, "pi_tombstoned_left_alone", "payment_intent.amount_capturable_updated")

        db_session.refresh(row)
        assert row.client_updated_at == removed_at, "the purchase re-stamped a line the customer had deleted"

        # A genuine re-add after the removal still wins.
        merge_cart(
            db_session,
            buyer,
            [
                CartLineMerge(
                    variant_id=variant.id,
                    quantity=2,
                    client_updated_at=_aware_utc(removed_at) + _td(minutes=1),
                )
            ],
        )
        assert [line.quantity for line in get_cart(db_session, buyer)] == [2]


class TestServerTombstonesReachTheClient:
    """get_cart is live-only, so a device pulling the basket cannot learn that
    a line it still holds live was deleted elsewhere - the variant is just
    absent, which a union merge reads as "unknown". The response carries the
    tombstones separately so the client can resolve that itself.
    """

    def test_get_cart_tombstones_returns_removed_lines_with_their_clock(self, db_session):
        from app.services.cart_service import (
            CartOwner,
            get_cart_tombstones,
            remove_line,
            set_line,
        )
        from app.models.cart_item import CartItem
        from sqlalchemy import select as _select

        variant = make_variant(db_session, "SYN-22", stock=5)
        owner = CartOwner.user(USER)
        set_line(db_session, owner, variant.id, 2)
        remove_line(db_session, owner, variant.id)

        tombstones = get_cart_tombstones(db_session, owner)

        assert [t.variant_id for t in tombstones] == [variant.id]
        row = db_session.scalars(
            _select(CartItem).where(CartItem.variant_id == variant.id)
        ).first()
        assert tombstones[0].client_updated_at == _aware_utc(row.client_updated_at)

    def test_a_live_line_is_not_reported_as_a_tombstone(self, db_session):
        from app.services.cart_service import CartOwner, get_cart_tombstones, set_line

        variant = make_variant(db_session, "SYN-23", stock=5)
        owner = CartOwner.user(USER)
        set_line(db_session, owner, variant.id, 1)

        assert get_cart_tombstones(db_session, owner) == []

    def test_an_old_tombstone_is_still_reported_until_the_sweep_removes_it(self, db_session):
        # get_cart_tombstones does not re-derive a retention window - the sweep
        # is the one authority on that, and its age is configurable. A device
        # offline longer than the default window still needs to learn about the
        # deletion, so anything the sweep has kept is returned.
        from app.services.cart_service import (
            CartOwner,
            get_cart_tombstones,
            remove_line,
            set_line,
        )
        from app.models.cart_item import CartItem
        from sqlalchemy import select as _select

        variant = make_variant(db_session, "SYN-24", stock=5)
        owner = CartOwner.user(USER)
        set_line(db_session, owner, variant.id, 1)
        remove_line(db_session, owner, variant.id)
        row = db_session.scalars(
            _select(CartItem).where(CartItem.variant_id == variant.id)
        ).first()
        row.deleted_at = datetime.now(tz=timezone.utc) - timedelta(days=200)
        db_session.commit()

        assert [t.variant_id for t in get_cart_tombstones(db_session, owner)] == [variant.id]

    def test_the_cart_response_carries_deleted_lines(self, user_client: TestClient, db_session):
        variant = make_variant(db_session, "SYN-25", stock=5)
        _sync(user_client, [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}])
        _sync(
            user_client,
            [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(NOW), "deleted": True}],
        )

        body = user_client.get("/api/v1/cart").json()

        assert body["items"] == []
        assert [line["variant_id"] for line in body["deleted_lines"]] == [str(variant.id)]

    def test_the_tombstone_list_is_capped_at_the_newest_deletion(self, db_session):
        # Ranked by deleted_at - the server's own clock, and the one that says
        # how recently a device could plausibly still need to learn about this
        # deletion.
        from app.services.cart_service import (
            MAX_TOMBSTONES_RETURNED,
            CartOwner,
            get_cart_tombstones,
            remove_line,
            set_line,
        )
        from app.models.cart_item import CartItem
        from sqlalchemy import select as _select

        owner = CartOwner.user(USER)
        for n in range(MAX_TOMBSTONES_RETURNED + 25):
            v = make_variant(db_session, f"SYN-CAP-{n}", stock=5)
            set_line(db_session, owner, v.id, 1)
            remove_line(db_session, owner, v.id)

        # Spread deleted_at, oldest first.
        rows = db_session.scalars(
            _select(CartItem).where(CartItem.owner_id == owner.id).order_by(CartItem.created_at)
        ).all()
        base = datetime.now(tz=timezone.utc)
        for i, row in enumerate(rows):
            row.deleted_at = base - timedelta(minutes=len(rows) - i)
        db_session.commit()
        cutoff = rows[-MAX_TOMBSTONES_RETURNED].deleted_at

        got = get_cart_tombstones(db_session, owner)

        assert len(got) == MAX_TOMBSTONES_RETURNED
        kept_ids = {t.variant_id for t in got}
        expected_ids = {
            row.variant_id for row in rows if _aware_utc(row.deleted_at) >= _aware_utc(cutoff)
        }
        assert kept_ids == expected_ids, "the cap kept the wrong deletions over the most recent ones"

    def test_the_cap_ranks_by_when_the_server_recorded_the_deletion_not_the_devices_own_clock(
        self, db_session
    ):
        """The regression this guards: a deletion synced late by a device that
        had been offline carries an *old* client_updated_at even though the
        tombstone itself is brand new. Ranking the cap by client_updated_at
        would treat that tombstone as stale and cut it first - exactly the one
        another device most needs to learn about."""
        from app.services.cart_service import (
            MAX_TOMBSTONES_RETURNED,
            CartOwner,
            get_cart_tombstones,
            remove_line,
            set_line,
        )
        from app.models.cart_item import CartItem
        from sqlalchemy import select as _select

        owner = CartOwner.user(USER)
        # Fill the cap with tombstones that are fresh by *every* clock - both
        # deleted_at and client_updated_at within the last hour.
        for n in range(MAX_TOMBSTONES_RETURNED):
            v = make_variant(db_session, f"SYN-CAP2-{n}", stock=5)
            set_line(db_session, owner, v.id, 1)
            remove_line(db_session, owner, v.id)
        filler_rows = db_session.scalars(
            _select(CartItem).where(CartItem.owner_id == owner.id)
        ).all()
        an_hour_ago = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        for row in filler_rows:
            row.deleted_at = an_hour_ago
            row.client_updated_at = an_hour_ago
        db_session.commit()

        # One more deletion, recorded by the server just now (the freshest
        # deleted_at of all 201) but carrying a client_updated_at from 30 days
        # ago, as a device that synced its deletion very late would.
        late = make_variant(db_session, "SYN-CAP2-LATE", stock=5)
        set_line(db_session, owner, late.id, 1)
        remove_line(db_session, owner, late.id)
        late_row = db_session.scalars(
            _select(CartItem).where(CartItem.variant_id == late.id)
        ).first()
        late_row.client_updated_at = datetime.now(tz=timezone.utc) - timedelta(days=30)
        # deleted_at is left alone - it is the server's own clock, and it is
        # newer than every filler's, which is exactly why this one must survive
        # the cap even though its client_updated_at looks 30 days stale.
        db_session.commit()

        got = get_cart_tombstones(db_session, owner)

        assert late.id in {t.variant_id for t in got}, (
            "the freshest deletion was capped out because its client clock looked old"
        )

    def test_a_sync_response_omits_deleted_lines(self, user_client: TestClient, db_session):
        # The client does not read a push response - it reconciles only at a
        # pull point - so PUT does not pay the tombstone query or payload.
        variant = make_variant(db_session, "SYN-27", stock=5)
        _sync(user_client, [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(EARLIER)}])

        body = _sync(
            user_client,
            [{"variant_id": str(variant.id), "quantity": 1, "client_updated_at": _iso(NOW), "deleted": True}],
        ).json()

        assert body["deleted_lines"] == []
        # ...but GET still surfaces it.
        assert [line["variant_id"] for line in user_client.get("/api/v1/cart").json()["deleted_lines"]] == [
            str(variant.id)
        ]

    def test_one_customer_cannot_see_another_customers_tombstones(self, db_session):
        from app.services.cart_service import (
            CartOwner,
            get_cart_tombstones,
            remove_line,
            set_line,
        )

        variant = make_variant(db_session, "SYN-26", stock=5)
        other = CartOwner.user("someone-else")
        set_line(db_session, other, variant.id, 1)
        remove_line(db_session, other, variant.id)

        assert get_cart_tombstones(db_session, CartOwner.user(USER)) == []


def _aware_utc(dt):
    from datetime import timezone as _tz

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_tz.utc)
