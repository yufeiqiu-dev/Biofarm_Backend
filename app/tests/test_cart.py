"""The basket lives on the server.

It used to live in `localStorage` under `cart:{sub}`, which ties a basket to a
device rather than to a person: fill one on a phone, sign in on a laptop, and it
is gone. That is also why the checkout page could not offer to recover anything -
a customer whose basket had been lost arrived with an empty cart and was bounced
straight back to it.
"""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.product import Product
from app.models.product_variant import ProductVariant

USER = "test-user-123"  # the sub user_client authenticates as


def make_variant(db_session, catalog_id: str, *, price="10.00", stock=5):
    product = Product(
        cat_id=f"P-{catalog_id}",
        name=f"Product {catalog_id}",
        description="A description",
        image_urls=[f"https://cdn.example.com/{catalog_id}.jpg"],
    )
    variant = ProductVariant(
        catalog_id=catalog_id,
        size_value=Decimal("50"),
        size_unit="ug",
        price=Decimal(price),
        stock=stock,
    )
    product.variants.append(variant)
    db_session.add(product)
    db_session.commit()
    db_session.refresh(variant)
    return variant


def test_a_new_customer_has_an_empty_basket(user_client: TestClient):
    body = user_client.get("/api/v1/cart").json()

    assert body["items"] == []
    assert body["subtotal"] == 0


def test_adding_a_line_returns_the_whole_basket(user_client: TestClient, db_session):
    # The whole basket, so the client never has to guess what the server now
    # holds or issue a second request to find out.
    variant = make_variant(db_session, "AB-101", price="285.00", stock=8)

    body = user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 2}).json()

    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["quantity"] == 2
    assert line["catalog_number"] == "AB-101"
    assert line["unit_price"] == 285.0
    assert body["subtotal"] == 570.0


def test_the_basket_survives_the_device(user_client: TestClient, db_session):
    """The whole point. A second request with no local state finds the basket."""
    variant = make_variant(db_session, "AB-102")
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 3})

    body = user_client.get("/api/v1/cart").json()

    assert body["items"][0]["quantity"] == 3


def test_setting_a_line_again_replaces_the_quantity(user_client: TestClient, db_session):
    # Absolute, not additive: "set this line to 3" is what a quantity box means.
    variant = make_variant(db_session, "AB-103", stock=9)
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 2})

    body = user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 3}).json()

    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 3


def test_setting_a_line_to_zero_removes_it(user_client: TestClient, db_session):
    # Rather than storing a line of nothing, which the database refuses anyway.
    variant = make_variant(db_session, "AB-104")
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 2})

    body = user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 0}).json()

    assert body["items"] == []


def test_prices_are_read_from_the_catalogue_not_the_line(user_client: TestClient, db_session):
    """A line stores a variant and a quantity, nothing else.

    Denormalising the price would leave a basket quoting what the product cost
    when it was added - and the customer would be charged something else at
    checkout, which is the disagreement this whole area keeps producing.
    """
    variant = make_variant(db_session, "AB-105", price="100.00")
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 2})

    variant.price = Decimal("120.00")
    db_session.commit()

    body = user_client.get("/api/v1/cart").json()

    assert body["items"][0]["unit_price"] == 120.0
    assert body["subtotal"] == 240.0


def test_a_line_over_stock_is_reported_not_trimmed(user_client: TestClient, db_session):
    """Quietly reducing a basket on a page load is how someone ends up buying
    fewer than they meant to and only finding out from the receipt."""
    variant = make_variant(db_session, "AB-106", stock=5)
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 5})

    variant.stock = 2
    db_session.commit()

    body = user_client.get("/api/v1/cart").json()

    assert body["items"][0]["quantity"] == 5, "the basket was silently trimmed"
    assert body["items"][0]["available"] == 2
    assert body["items"][0]["over_stock"] is True
    assert body["unavailable"] == ["AB-106"]


def test_a_basket_within_stock_reports_nothing_unavailable(user_client: TestClient, db_session):
    variant = make_variant(db_session, "AB-107", stock=5)
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 5})

    body = user_client.get("/api/v1/cart").json()

    assert body["unavailable"] == []
    assert body["items"][0]["over_stock"] is False


def test_a_line_can_be_removed(user_client: TestClient, db_session):
    variant = make_variant(db_session, "AB-108")
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 1})

    body = user_client.delete(f"/api/v1/cart/items/{variant.id}").json()

    assert body["items"] == []


def test_removing_a_line_twice_is_not_an_error(user_client: TestClient, db_session):
    # The caller wanted it gone and it is gone. A 404 on the second press would
    # make a double-click look like a failure.
    variant = make_variant(db_session, "AB-109")
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 1})
    user_client.delete(f"/api/v1/cart/items/{variant.id}")

    assert user_client.delete(f"/api/v1/cart/items/{variant.id}").status_code == 200


def test_the_basket_can_be_emptied(user_client: TestClient, db_session):
    first = make_variant(db_session, "AB-110")
    second = make_variant(db_session, "AB-111")
    user_client.put(f"/api/v1/cart/items/{first.id}", json={"quantity": 1})
    user_client.put(f"/api/v1/cart/items/{second.id}", json={"quantity": 1})

    assert user_client.delete("/api/v1/cart").status_code == 204
    assert user_client.get("/api/v1/cart").json()["items"] == []


def test_a_variant_that_does_not_exist_is_refused(user_client: TestClient):
    import uuid as _uuid

    response = user_client.put(
        f"/api/v1/cart/items/{_uuid.uuid4()}", json={"quantity": 1}
    )

    assert response.status_code == 404


def test_a_negative_quantity_is_rejected(user_client: TestClient, db_session):
    variant = make_variant(db_session, "AB-112")

    response = user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": -1})

    assert response.status_code == 422


def test_an_absurd_quantity_is_rejected(user_client: TestClient, db_session):
    # An unbounded quantity makes the tax call and the authorisation absurd.
    variant = make_variant(db_session, "AB-113")

    response = user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 100000})

    assert response.status_code == 422


def test_a_basket_needs_a_signed_in_customer(client: TestClient):
    """Every route is scoped to the caller's own sub. Without that, the cart id
    in a URL would be a way to read and edit somebody else's basket."""
    assert client.get("/api/v1/cart").status_code in (401, 403)


def test_one_customer_cannot_see_another_basket(user_client: TestClient, db_session):
    """The rows are owned, and the owner comes from the token rather than the
    request."""
    from app.services.cart_service import CartOwner, get_cart, set_line

    variant = make_variant(db_session, "AB-114")
    set_line(db_session, CartOwner.user("someone-else"), variant.id, 4)

    body = user_client.get("/api/v1/cart").json()

    assert body["items"] == [], "another customer's basket leaked into this one"
    assert len(get_cart(db_session, CartOwner.user("someone-else"))) == 1


def test_guest_and_user_baskets_do_not_collide(db_session):
    """owner_kind is stored rather than inferred from the shape of the id.

    Guest checkout is the next feature and will put a cart token in owner_id;
    telling the two apart by guessing would stop being true quietly, and guest
    baskets need sweeping while a customer's does not.
    """
    from app.services.cart_service import CartOwner, OWNER_GUEST, get_cart, set_line

    variant = make_variant(db_session, "AB-115")
    same_id = "collides"
    set_line(db_session, CartOwner.user(same_id), variant.id, 2)
    set_line(db_session, CartOwner(kind=OWNER_GUEST, id=same_id), variant.id, 7)

    assert get_cart(db_session, CartOwner.user(same_id))[0].quantity == 2
    assert get_cart(db_session, CartOwner(kind=OWNER_GUEST, id=same_id))[0].quantity == 7


def _checkout(user_client, variant, quantity: int):
    """Post a checkout for one variant, with Stripe patched at the endpoint's
    imported names - the seam every other checkout test uses."""
    from unittest.mock import patch

    from app.tests.test_orders import make_stripe_pi_mock, make_tax_mock

    payload = {
        "cart": [{"variant_id": str(variant.id), "quantity": quantity}],
        "shipping": {
            "name": "Jane Smith",
            "phone": "5551234567",
            "address1": "1 Research Park",
            "city": "Springfield",
            "state": "IL",
            "zip": "62701",
        },
    }
    with patch(
        "app.api.v1.endpoints.orders.create_payment_intent",
        return_value=make_stripe_pi_mock(),
    ), patch(
        "app.api.v1.endpoints.orders.calculate_tax", return_value=make_tax_mock(2000)
    ):
        return user_client.post("/api/v1/orders/payment-intent", json=payload)


def test_starting_a_checkout_does_not_empty_the_basket(user_client: TestClient, db_session):
    """Checkout creates a session, not an order - the order appears when the
    webhook lands. Emptying the basket here would take it from a customer who
    never went on to pay."""
    from app.services.cart_service import CartOwner, get_cart

    variant = make_variant(db_session, "AB-116", price="10.00", stock=5)
    user_client.put(f"/api/v1/cart/items/{variant.id}", json={"quantity": 2})

    response = _checkout(user_client, variant, 2)

    assert response.status_code == 201, response.text
    assert len(get_cart(db_session, CartOwner.user(USER))) == 1


def test_the_order_takes_the_basket_with_it(client: TestClient, db_session):
    """The basket goes with the order that bought it.

    Without this a customer pays and is left holding a basket of exactly what
    they just paid for - and the next checkout charges them for it again.
    """
    from app.services.cart_service import CartOwner, get_cart, set_line
    from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

    variant = make_variant(db_session, "AB-117", stock=5)
    # "wh-user" is the sub make_checkout_session records as the buyer.
    buyer = CartOwner.user("wh-user")
    set_line(db_session, buyer, variant.id, 1)
    make_checkout_session(db_session, "pi_cart_test", variant.id)

    response = post_webhook(client, "pi_cart_test", "payment_intent.amount_capturable_updated")

    assert response.status_code == 200
    assert get_cart(db_session, buyer) == [], "the basket outlived the order"


def test_another_customer_keeps_their_basket(client: TestClient, db_session):
    """Only the buyer's basket is emptied."""
    from app.services.cart_service import CartOwner, get_cart, set_line
    from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

    variant = make_variant(db_session, "AB-118", stock=5)
    set_line(db_session, CartOwner.user("wh-user"), variant.id, 1)
    set_line(db_session, CartOwner.user("someone-else"), variant.id, 2)
    make_checkout_session(db_session, "pi_cart_test_2", variant.id)

    post_webhook(client, "pi_cart_test_2", "payment_intent.amount_capturable_updated")

    assert len(get_cart(db_session, CartOwner.user("someone-else"))) == 1


def test_only_the_bought_lines_are_cleared(client: TestClient, db_session):
    """A basket added to from another device while the payment was in flight
    keeps what nobody paid for.

    Cross-device continuity is the point of saving the basket at all, which
    makes this ordinary rather than exotic: checkout starts on a laptop with one
    item, a phone adds another, and the webhook lands.
    """
    from app.services.cart_service import CartOwner, get_cart, set_line
    from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

    bought = make_variant(db_session, "AB-119", stock=5)
    added_later = make_variant(db_session, "AB-120", stock=5)
    buyer = CartOwner.user("wh-user")
    set_line(db_session, buyer, bought.id, 1)
    set_line(db_session, buyer, added_later.id, 2)
    make_checkout_session(db_session, "pi_partial_clear", bought.id)

    post_webhook(client, "pi_partial_clear", "payment_intent.amount_capturable_updated")

    remaining = get_cart(db_session, buyer)
    assert [line.variant.catalog_id for line in remaining] == ["AB-120"]
    assert remaining[0].quantity == 2


def test_an_unknown_variant_is_refused_whatever_the_quantity(user_client: TestClient):
    """The same id must not answer 404 or 200 depending on the number sent, or
    the endpoint cannot be used to tell whether a variant exists."""
    import uuid as _uuid

    missing = _uuid.uuid4()

    assert user_client.put(f"/api/v1/cart/items/{missing}", json={"quantity": 1}).status_code == 404
    assert user_client.put(f"/api/v1/cart/items/{missing}", json={"quantity": 0}).status_code == 404


def test_only_the_bought_quantity_leaves_the_basket(client: TestClient, db_session):
    """Units added while the payment was in flight are not paid for.

    make_checkout_session buys one unit. If the basket has since gone up to
    three, two of them belong to the customer still - deleting the whole line
    takes them.
    """
    from app.services.cart_service import CartOwner, get_cart, set_line
    from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

    variant = make_variant(db_session, "AB-121", stock=9)
    buyer = CartOwner.user("wh-user")
    set_line(db_session, buyer, variant.id, 3)
    make_checkout_session(db_session, "pi_partial_qty", variant.id)

    post_webhook(client, "pi_partial_qty", "payment_intent.amount_capturable_updated")

    remaining = get_cart(db_session, buyer)
    assert len(remaining) == 1
    assert remaining[0].quantity == 2, "the unpaid units were taken too"


def test_a_line_bought_in_full_leaves_the_basket(client: TestClient, db_session):
    from app.services.cart_service import CartOwner, get_cart, set_line
    from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

    variant = make_variant(db_session, "AB-122", stock=9)
    buyer = CartOwner.user("wh-user")
    set_line(db_session, buyer, variant.id, 1)
    make_checkout_session(db_session, "pi_full_qty", variant.id)

    post_webhook(client, "pi_full_qty", "payment_intent.amount_capturable_updated")

    assert get_cart(db_session, buyer) == []


def test_a_basket_that_will_not_empty_does_not_fail_the_order(client: TestClient, db_session, monkeypatch):
    """The order is already durable by then.

    Sharing the commit meant a lock timeout on a cart row - the customer's other
    device editing the basket at that moment - failed the whole webhook, and
    Stripe retries a webhook whose CheckoutSession is still there: a second
    order for one payment.
    """
    from app.models.order import Order
    from app.services import cart_service
    from app.tests.test_stripe_webhook import make_checkout_session, post_webhook

    variant = make_variant(db_session, "AB-123", stock=5)
    make_checkout_session(db_session, "pi_cart_boom", variant.id)

    def explode(*args, **kwargs):
        raise RuntimeError("cart row locked")

    # The inner call, so the best-effort wrapper's own try/except is what is
    # under test.
    monkeypatch.setattr(cart_service, "clear_bought_lines", explode)

    response = post_webhook(client, "pi_cart_boom", "payment_intent.amount_capturable_updated")

    assert response.status_code == 200, "Stripe would retry and duplicate the order"
    assert db_session.scalar(
        select(Order).where(Order.stripe_payment_intent_id == "pi_cart_boom")
    ) is not None
