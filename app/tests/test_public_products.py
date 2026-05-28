import uuid

import pytest
from fastapi.testclient import TestClient

from app.models.product import Product
from app.models.product_variant import ProductVariant


def make_product(cat_id: str, name: str = "Test Product") -> Product:
    return Product(cat_id=cat_id, name=name, description="A test product")


def make_variant(catalog_id: str, price: float = 10.0, stock: int = 5) -> ProductVariant:
    return ProductVariant(
        catalog_id=catalog_id,
        size_value=10,
        size_unit="mL",
        price=price,
        stock=stock,
    )


# --- GET /api/v1/products ---

def test_list_products_empty(client: TestClient):
    response = client.get("/api/v1/products")
    assert response.status_code == 200
    assert response.json() == []


def test_list_products_excludes_products_without_variants(client: TestClient, db_session):
    product = make_product("NO-VAR")
    db_session.add(product)
    db_session.commit()

    response = client.get("/api/v1/products")
    assert response.status_code == 200
    assert response.json() == []


def test_list_products_includes_products_with_variants(client: TestClient, db_session):
    product = make_product("WITH-VAR", name="Reagent A")
    product.variants.append(make_variant("WITH-VAR-01"))
    db_session.add(product)
    db_session.commit()

    response = client.get("/api/v1/products")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["cat_id"] == "WITH-VAR"
    assert data[0]["name"] == "Reagent A"
    assert len(data[0]["variants"]) == 1
    assert data[0]["variants"][0]["catalog_id"] == "WITH-VAR-01"


def test_list_products_mixed(client: TestClient, db_session):
    p_with = make_product("MIX-A", name="Has Variants")
    p_with.variants.append(make_variant("MIX-A-01"))

    p_without = make_product("MIX-B", name="No Variants")

    db_session.add_all([p_with, p_without])
    db_session.commit()

    response = client.get("/api/v1/products")
    assert response.status_code == 200
    data = response.json()
    cat_ids = [p["cat_id"] for p in data]
    assert "MIX-A" in cat_ids
    assert "MIX-B" not in cat_ids


def test_list_products_returns_all_variants(client: TestClient, db_session):
    product = make_product("MULTI-VAR", name="Multi Variant")
    product.variants.append(make_variant("MULTI-VAR-01", price=10.0))
    product.variants.append(make_variant("MULTI-VAR-02", price=20.0))
    db_session.add(product)
    db_session.commit()

    response = client.get("/api/v1/products")
    assert response.status_code == 200
    data = response.json()
    assert len(data[0]["variants"]) == 2


def test_list_products_ordered_by_name(client: TestClient, db_session):
    p_b = make_product("ORD-B", name="Beta")
    p_b.variants.append(make_variant("ORD-B-01"))

    p_a = make_product("ORD-A", name="Alpha")
    p_a.variants.append(make_variant("ORD-A-01"))

    db_session.add_all([p_b, p_a])
    db_session.commit()

    response = client.get("/api/v1/products")
    assert response.status_code == 200
    names = [p["name"] for p in response.json()]
    assert names == sorted(names)


# --- GET /api/v1/products/{product_id} ---

def test_get_product_by_id(client: TestClient, db_session):
    product = make_product("DETAIL-01", name="Detail Product")
    product.variants.append(make_variant("DETAIL-01-A", price=25.0, stock=3))
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    response = client.get(f"/api/v1/products/{product.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["cat_id"] == "DETAIL-01"
    assert data["name"] == "Detail Product"
    assert data["description"] == "A test product"
    assert len(data["variants"]) == 1
    assert data["variants"][0]["catalog_id"] == "DETAIL-01-A"
    assert float(data["variants"][0]["price"]) == 25.0


def test_get_product_by_id_returns_product_without_variants(client: TestClient, db_session):
    # The detail endpoint returns the product regardless of variant count
    # (frontend handles the empty-variants state)
    product = make_product("NO-VAR-DETAIL")
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    response = client.get(f"/api/v1/products/{product.id}")
    assert response.status_code == 200
    assert response.json()["variants"] == []


def test_get_product_not_found(client: TestClient):
    response = client.get(f"/api/v1/products/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Product not found"


def test_get_product_invalid_uuid(client: TestClient):
    response = client.get("/api/v1/products/not-a-valid-uuid")
    assert response.status_code == 422
