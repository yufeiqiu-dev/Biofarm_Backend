"""Stock is adjusted, not overwritten.

Stock left the product form because the form is a read-modify-write over a whole
product: it reads every field, the admin edits one, and it writes them all back -
including a stock count read before they started typing. Every sale made while
the page was open was overwritten by it. Proven against Postgres as shelf 4,
customer buys 2, admin edits only the price, system says 4 and the shelf holds 2.

The lock in adjust_variant_stock is not about two admins - there is one. It is
about the admin and the customers.
"""

from fastapi.testclient import TestClient

from app.tests.test_admin_products import make_product, make_variant


def _stock_url(product, variant):
    return f"/api/v1/admin/products/{product.id}/variants/{variant.id}/stock"


def _one_variant(db_session, cat_id: str, stock: int):
    product = make_product(cat_id)
    product.variants.append(make_variant(f"{cat_id}-A", stock=stock))
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product, product.variants[0]


def test_a_delivery_adds_to_the_count(admin_client: TestClient, db_session):
    product, variant = _one_variant(db_session, "STK-01", stock=4)

    response = admin_client.post(_stock_url(product, variant), json={"delta": 12})

    assert response.status_code == 200
    assert response.json()["stock"] == 16
    db_session.refresh(variant)
    assert variant.stock == 16


def test_a_correction_can_go_down(admin_client: TestClient, db_session):
    """Breakage and miscounts are the same operation as a delivery. A separate
    "set" action would reintroduce the overwrite this exists to remove."""
    product, variant = _one_variant(db_session, "STK-02", stock=10)

    response = admin_client.post(_stock_url(product, variant), json={"delta": -3})

    assert response.status_code == 200
    assert response.json()["stock"] == 7


def test_a_sale_during_the_edit_is_kept(admin_client: TestClient, db_session):
    """The whole reason for the endpoint.

    The admin opens the page at 4, a customer buys 2, and the admin then records
    a delivery of 12. The answer is 14 - the shelf - not 16, which is what
    applying that delivery to the number on their screen would have given.
    """
    product, variant = _one_variant(db_session, "STK-03", stock=4)

    # The sale, after the page was rendered and before the admin submits.
    variant.stock = 2
    db_session.commit()

    response = admin_client.post(_stock_url(product, variant), json={"delta": 12})

    assert response.json()["stock"] == 14


def test_stock_cannot_be_driven_negative(admin_client: TestClient, db_session):
    product, variant = _one_variant(db_session, "STK-04", stock=2)

    response = admin_client.post(_stock_url(product, variant), json={"delta": -5})

    assert response.status_code == 400
    # Reported against the shelf now, which need not be what the admin saw -
    # that being the reason this endpoint exists at all.
    assert "stock of 2" in response.json()["detail"]
    db_session.refresh(variant)
    assert variant.stock == 2


def test_a_zero_adjustment_is_rejected(admin_client: TestClient, db_session):
    product, variant = _one_variant(db_session, "STK-05", stock=1)

    response = admin_client.post(_stock_url(product, variant), json={"delta": 0})

    assert response.status_code == 422


def test_a_variant_from_another_product_is_not_found(admin_client: TestClient, db_session):
    """The product in the path has to own the variant, or the URL is a way to
    adjust any variant in the catalogue by guessing ids."""
    p1, v1 = _one_variant(db_session, "STK-06", stock=5)
    p2, _ = _one_variant(db_session, "STK-07", stock=5)

    response = admin_client.post(
        f"/api/v1/admin/products/{p2.id}/variants/{v1.id}/stock",
        json={"delta": 1},
    )

    assert response.status_code == 404
    db_session.refresh(v1)
    assert v1.stock == 5


def test_restocking_needs_an_admin(client: TestClient, db_session):
    product, variant = _one_variant(db_session, "STK-08", stock=1)

    response = client.post(_stock_url(product, variant), json={"delta": 100})

    assert response.status_code in (401, 403)


def test_the_product_form_cannot_write_stock(admin_client: TestClient, db_session):
    """Rejected, not ignored - a client still sending it is told, rather than
    watching its writes disappear."""
    product, variant = _one_variant(db_session, "STK-09", stock=3)

    response = admin_client.put(f"/api/v1/admin/products/{product.id}", json={
        "variants": [{
            "id": str(variant.id), "catalog_id": variant.catalog_id,
            "size_value": 1, "size_unit": "mL", "price": 1.0, "stock": 99,
        }]
    })

    assert response.status_code == 422
    db_session.refresh(variant)
    assert variant.stock == 3, "the count must be untouched by a rejected write"


def test_editing_a_product_leaves_stock_alone(admin_client: TestClient, db_session):
    """The proven bug: an admin edits only a description, and stock reverts to
    whatever the page happened to be rendered with."""
    product, variant = _one_variant(db_session, "STK-10", stock=4)

    variant.stock = 2  # two sold while the form was open
    db_session.commit()

    response = admin_client.put(f"/api/v1/admin/products/{product.id}", json={
        "description": "A corrected description",
        "variants": [{
            "id": str(variant.id), "catalog_id": variant.catalog_id,
            "size_value": 1, "size_unit": "mL", "price": 1.0,
        }]
    })

    assert response.status_code == 200
    db_session.refresh(variant)
    assert variant.stock == 2, "the sale was overwritten by the form"


def test_a_new_variant_still_needs_an_opening_count(admin_client: TestClient, db_session):
    """Write-once, not never: an inserted variant has no count to adjust yet."""
    product, _ = _one_variant(db_session, "STK-11", stock=1)

    without = admin_client.put(f"/api/v1/admin/products/{product.id}", json={
        "variants": [{
            "catalog_id": "STK-11-B", "size_value": 5, "size_unit": "mL", "price": 2.0,
        }]
    })
    assert without.status_code == 422

    with_stock = admin_client.put(f"/api/v1/admin/products/{product.id}", json={
        "variants": [{
            "catalog_id": "STK-11-B", "size_value": 5, "size_unit": "mL",
            "price": 2.0, "stock": 7,
        }]
    })
    assert with_stock.status_code == 200
    assert with_stock.json()["variants"][0]["stock"] == 7


def test_the_reason_is_recorded(admin_client: TestClient, db_session, caplog):
    """A count that changed with no record of why is what this endpoint exists
    to stop. Not an audit table - one admin does not need one - but the delta,
    the resulting count and the reason are in the log."""
    import logging

    product, variant = _one_variant(db_session, "STK-12", stock=4)

    with caplog.at_level(logging.INFO, logger="app.services.product_service"):
        admin_client.post(
            _stock_url(product, variant),
            json={"delta": 12, "reason": "delivery 4471"},
        )

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "delivery 4471" in logged
    assert "+12" in logged
    assert "now=16" in logged


def test_not_found_is_a_type_not_a_message(admin_client: TestClient, db_session):
    """The endpoint used to compare the error text to decide 404 versus 400, so
    rewording it - a typo fix, adding the id - would quietly have turned a real
    404 into a 400. It is a subclass of ValueError so the endpoint's generic
    branch still catches anything it does not name."""
    from app.services.product_service import VariantNotFound

    assert issubclass(VariantNotFound, ValueError)
