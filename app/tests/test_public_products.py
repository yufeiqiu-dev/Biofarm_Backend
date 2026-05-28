import uuid

import pytest
from fastapi.testclient import TestClient

from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.tag import Tag


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


# --- Search and tag filtering ---

def test_search_by_name(client: TestClient, db_session):
    p1 = make_product("SRCH-A", name="Wash Buffer")
    p1.variants.append(make_variant("SRCH-A-01"))
    p2 = make_product("SRCH-B", name="Flow Cytometry Kit")
    p2.variants.append(make_variant("SRCH-B-01"))
    db_session.add_all([p1, p2])
    db_session.commit()

    response = client.get("/api/v1/products?search=wash")
    assert response.status_code == 200
    names = [p["name"] for p in response.json()]
    assert "Wash Buffer" in names
    assert "Flow Cytometry Kit" not in names


def test_search_by_description(client: TestClient, db_session):
    p = make_product("SRCH-C", name="Reagent X")
    p.description = "Used for western blot analysis"
    p.variants.append(make_variant("SRCH-C-01"))
    db_session.add(p)
    db_session.commit()

    response = client.get("/api/v1/products?search=western+blot")
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_search_is_case_insensitive(client: TestClient, db_session):
    p = make_product("SRCH-D", name="ELISA Kit")
    p.variants.append(make_variant("SRCH-D-01"))
    db_session.add(p)
    db_session.commit()

    response = client.get("/api/v1/products?search=elisa")
    assert response.status_code == 200
    assert any(p["name"] == "ELISA Kit" for p in response.json())


def test_filter_by_tag(client: TestClient, db_session):
    t_antibody = Tag(name="antibody")
    t_primary = Tag(name="primary")
    t_buffer = Tag(name="buffer")
    p1 = make_product("TAG-A", name="Antibody A")
    p1.tags = [t_antibody, t_primary]
    p1.variants.append(make_variant("TAG-A-01"))
    p2 = make_product("TAG-B", name="Buffer B")
    p2.tags = [t_buffer]
    p2.variants.append(make_variant("TAG-B-01"))
    db_session.add_all([t_antibody, t_primary, t_buffer, p1, p2])
    db_session.commit()

    response = client.get("/api/v1/products?tags=antibody")
    assert response.status_code == 200
    names = [p["name"] for p in response.json()]
    assert "Antibody A" in names
    assert "Buffer B" not in names


def test_filter_by_multiple_tags(client: TestClient, db_session):
    t_antibody = Tag(name="antibody2")
    t_primary = Tag(name="primary2")
    t_secondary = Tag(name="secondary2")
    p1 = make_product("MTAG-A", name="Primary Antibody")
    p1.tags = [t_antibody, t_primary]
    p1.variants.append(make_variant("MTAG-A-01"))
    p2 = make_product("MTAG-B", name="Secondary Antibody")
    p2.tags = [t_antibody, t_secondary]
    p2.variants.append(make_variant("MTAG-B-01"))
    db_session.add_all([t_antibody, t_primary, t_secondary, p1, p2])
    db_session.commit()

    response = client.get("/api/v1/products?tags=antibody2&tags=primary2")
    assert response.status_code == 200
    names = [p["name"] for p in response.json()]
    assert "Primary Antibody" in names
    assert "Secondary Antibody" not in names


def test_product_tags_returned_in_response(client: TestClient, db_session):
    t_kit = Tag(name="kit")
    t_elisa = Tag(name="elisa")
    p = make_product("TAGS-OUT", name="Tagged Product")
    p.tags = [t_kit, t_elisa]
    p.variants.append(make_variant("TAGS-OUT-01"))
    db_session.add_all([t_kit, t_elisa, p])
    db_session.commit()

    response = client.get("/api/v1/products")
    assert response.status_code == 200
    product = next(p for p in response.json() if p["cat_id"] == "TAGS-OUT")
    tag_names = {t["name"] for t in product["tags"]}
    assert tag_names == {"kit", "elisa"}
