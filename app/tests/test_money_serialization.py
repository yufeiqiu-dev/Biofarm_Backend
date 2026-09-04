"""Money must reach the browser as a JSON number, everywhere.

The bug this closes was invisible from either side alone. The backend typed
order money as `Decimal` and product prices as `float`; pydantic encodes the
first as a *string* and the second as a number, so the same API answered
`"19.99"` in one place and `19.99` in another. The frontend declared both
`number`, which TypeScript cannot check at runtime, and it worked only because
most call sites had accumulated a defensive `Number(...)` and the rest happened
to use `*`, which coerces. The first `total_amount.toFixed(2)` written without
the wrapper would have thrown in production.

These assert the wire format directly - json.loads gives Python types, so a
string that should be a number fails here rather than in a browser.
"""

import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from app.models.order import OrderStatus
from app.tests.test_orders import make_order, make_product_with_variant


def _variant_payload(catalog_id: str, price: str, size: str) -> dict:
    return {
        "catalog_id": catalog_id,
        "size_value": size,
        "size_unit": "g",
        "price": price,
        "stock": 5,
    }


# --- products ---

def test_public_product_prices_are_json_numbers(client: TestClient, db_session):
    make_product_with_variant(db_session, "MON-01", price=19.99)

    body = client.get("/api/v1/products").json()

    variant = body[0]["variants"][0]
    assert isinstance(variant["price"], float), f"price came back as {type(variant['price'])}"
    assert variant["price"] == 19.99
    assert isinstance(variant["size_value"], float)


def test_admin_product_prices_are_json_numbers(admin_client: TestClient, db_session):
    make_product_with_variant(db_session, "MON-02", price=5.5)

    body = admin_client.get("/api/v1/admin/products").json()

    assert isinstance(body[0]["variants"][0]["price"], float)


# --- orders ---

def test_order_money_fields_are_json_numbers(user_client: TestClient, db_session):
    make_order(db_session)

    body = user_client.get("/api/v1/orders").json()

    order = body[0]
    assert isinstance(order["total_amount"], float), "total_amount is still a string"
    assert isinstance(order["tax_amount"], float)
    assert isinstance(order["items"][0]["unit_price"], float)
    assert order["total_amount"] == 10.00


def test_admin_order_money_fields_are_json_numbers(admin_client: TestClient, db_session):
    order, _ = make_order(db_session)

    body = admin_client.get(f"/api/v1/admin/orders/{order.id}").json()

    assert isinstance(body["total_amount"], float)
    assert isinstance(body["tax_amount"], float)
    assert isinstance(body["items"][0]["unit_price"], float)


def test_two_decimal_places_survive_the_round_trip(user_client: TestClient, db_session):
    """The reason the values stay Decimal inside the app: a price must come back
    as the price that went in, not the nearest binary float to it."""
    order, _ = make_order(db_session)
    order.total_amount = Decimal("1234567.89")
    order.tax_amount = Decimal("0.01")
    db_session.commit()

    body = user_client.get("/api/v1/orders").json()[0]

    assert body["total_amount"] == 1234567.89
    assert body["tax_amount"] == 0.01


# --- input side ---

def test_price_with_more_precision_than_the_column_is_rejected(admin_client: TestClient):
    """Numeric(10, 2) cannot hold 19.999. Typed `float` this bound to the column
    and was silently rounded by the database; typed Decimal it is a 422."""
    response = admin_client.post(
        "/api/v1/admin/products",
        json={
            "cat_id": "MON-03",
            "name": "Too precise",
            "description": "x",
            "variants": [_variant_payload("MON-03-V1", price="19.999", size="100")],
        },
    )
    assert response.status_code == 422


def test_negative_price_is_rejected(admin_client: TestClient):
    response = admin_client.post(
        "/api/v1/admin/products",
        json={
            "cat_id": "MON-04",
            "name": "Negative",
            "description": "x",
            "variants": [_variant_payload("MON-04-V1", price="-1.00", size="100")],
        },
    )
    assert response.status_code == 422


def test_zero_size_is_rejected(admin_client: TestClient):
    response = admin_client.post(
        "/api/v1/admin/products",
        json={
            "cat_id": "MON-05",
            "name": "Sizeless",
            "description": "x",
            "variants": [_variant_payload("MON-05-V1", price="1.00", size="0")],
        },
    )
    assert response.status_code == 422


def test_a_normal_price_is_stored_exactly(admin_client: TestClient, db_session):
    response = admin_client.post(
        "/api/v1/admin/products",
        json={
            "cat_id": "MON-06",
            "name": "Fine",
            "description": "x",
            "variants": [_variant_payload("MON-06-V1", price="19.99", size="500")],
        },
    )
    assert response.status_code == 201

    from app.models.product_variant import ProductVariant
    variant = db_session.query(ProductVariant).filter_by(catalog_id="MON-06-V1").one()
    assert variant.price == Decimal("19.99")
    assert response.json()["variants"][0]["price"] == 19.99


def test_a_json_number_price_is_not_mangled_by_the_float_round_trip(admin_client: TestClient):
    """The path the admin editor actually takes.

    AdminProductDetailPage sends `price: Number(variant.price)`, so the body
    carries a JSON *number*, not a string. Naively that is alarming against a
    `decimal_places=2` bound - `Decimal(19.99)` is 19.98999999999999843...,
    which has far more than two. Pydantic parses the JSON literal's own text
    rather than round-tripping through a binary float, so 19.99 arrives as
    Decimal("19.99") and validates. Asserted here because the failure mode if
    that ever changed is every product save returning 422.
    """
    response = admin_client.post(
        "/api/v1/admin/products",
        json={
            "cat_id": "MON-07",
            "name": "From the editor",
            "description": "x",
            "variants": [
                {
                    "catalog_id": "MON-07-V1",
                    "size_value": 2.5,
                    "size_unit": "mL",
                    "price": 19.99,
                    "stock": 5,
                }
            ],
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["variants"][0]["price"] == 19.99


def test_an_amount_too_large_for_the_column_is_rejected(admin_client: TestClient):
    """Numeric(10, 2) tops out below 100,000,000."""
    response = admin_client.post(
        "/api/v1/admin/products",
        json={
            "cat_id": "MON-08",
            "name": "Too big",
            "description": "x",
            "variants": [_variant_payload("MON-08-V1", price="10000000000.00", size="100")],
        },
    )
    assert response.status_code == 422
