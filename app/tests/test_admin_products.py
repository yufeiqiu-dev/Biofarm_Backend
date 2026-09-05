import uuid
from unittest.mock import patch

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


def create_payload(**overrides) -> dict:
    base = {
        "cat_id": "NEW-01",
        "name": "New Product",
        "description": "A new product",
        "variants": [
            {"catalog_id": "NEW-01-A", "size_value": 10, "size_unit": "mL", "price": 20.0, "stock": 5}
        ],
    }
    return {**base, **overrides}


# --- POST /api/v1/admin/products ---

def test_create_product_success(admin_client: TestClient):
    response = admin_client.post("/api/v1/admin/products", json=create_payload())
    assert response.status_code == 201
    data = response.json()
    assert data["cat_id"] == "NEW-01"
    assert len(data["variants"]) == 1


def test_create_product_duplicate_cat_id(admin_client: TestClient, db_session):
    existing = make_product("DUP-CAT")
    existing.variants.append(make_variant("DUP-CAT-01"))
    db_session.add(existing)
    db_session.commit()

    response = admin_client.post("/api/v1/admin/products", json=create_payload(cat_id="DUP-CAT"))
    assert response.status_code == 409


def test_create_product_duplicate_catalog_id(admin_client: TestClient, db_session):
    existing = make_product("ORIG")
    existing.variants.append(make_variant("SHARED-CATALOG-ID"))
    db_session.add(existing)
    db_session.commit()

    payload = create_payload(cat_id="NEW-UNIQUE", variants=[
        {"catalog_id": "SHARED-CATALOG-ID", "size_value": 5, "size_unit": "g", "price": 10.0, "stock": 3}
    ])
    response = admin_client.post("/api/v1/admin/products", json=payload)
    assert response.status_code == 409


def test_create_product_with_no_variants_is_allowed(admin_client: TestClient):
    response = admin_client.post("/api/v1/admin/products", json=create_payload(variants=[]))
    assert response.status_code == 201
    assert response.json()["variants"] == []


def test_create_product_missing_required_fields(admin_client: TestClient):
    response = admin_client.post("/api/v1/admin/products", json={"cat_id": "X"})
    assert response.status_code == 422


def test_create_product_variant_price_cannot_be_negative(admin_client: TestClient):
    payload = create_payload(variants=[
        {"catalog_id": "NEG-01", "size_value": 10, "size_unit": "mL", "price": -1.0, "stock": 5}
    ])
    response = admin_client.post("/api/v1/admin/products", json=payload)
    assert response.status_code == 422


def test_create_product_variant_stock_cannot_be_negative(admin_client: TestClient):
    payload = create_payload(variants=[
        {"catalog_id": "NEG-STOCK", "size_value": 10, "size_unit": "mL", "price": 10.0, "stock": -1}
    ])
    response = admin_client.post("/api/v1/admin/products", json=payload)
    assert response.status_code == 422


def test_create_product_variant_size_must_be_positive(admin_client: TestClient):
    payload = create_payload(variants=[
        {"catalog_id": "ZERO-SIZE", "size_value": 0, "size_unit": "mL", "price": 10.0, "stock": 5}
    ])
    response = admin_client.post("/api/v1/admin/products", json=payload)
    assert response.status_code == 422


# --- PUT /api/v1/admin/products/{id} ---

def test_update_product_success(admin_client: TestClient, db_session):
    product = make_product("UPD-01", name="Original")
    product.variants.append(make_variant("UPD-01-A"))
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    response = admin_client.put(f"/api/v1/admin/products/{product.id}", json={"name": "Updated"})
    assert response.status_code == 200
    assert response.json()["name"] == "Updated"


def test_update_product_not_found(admin_client: TestClient):
    response = admin_client.put(f"/api/v1/admin/products/{uuid.uuid4()}", json={"name": "X"})
    assert response.status_code == 404


def test_update_product_duplicate_cat_id(admin_client: TestClient, db_session):
    p1 = make_product("TAKEN-ID")
    p1.variants.append(make_variant("TAKEN-ID-01"))
    p2 = make_product("TO-UPDATE")
    p2.variants.append(make_variant("TO-UPDATE-01"))
    db_session.add_all([p1, p2])
    db_session.commit()
    db_session.refresh(p2)

    response = admin_client.put(f"/api/v1/admin/products/{p2.id}", json={"cat_id": "TAKEN-ID"})
    assert response.status_code == 409


def test_update_product_duplicate_catalog_id_on_new_variant(admin_client: TestClient, db_session):
    existing = make_product("EXISTING")
    existing.variants.append(make_variant("TAKEN-CATALOG"))
    product = make_product("MY-PROD")
    product.variants.append(make_variant("MY-PROD-01"))
    db_session.add_all([existing, product])
    db_session.commit()
    db_session.refresh(product)

    response = admin_client.put(f"/api/v1/admin/products/{product.id}", json={
        "variants": [{"catalog_id": "TAKEN-CATALOG", "size_value": 10, "size_unit": "mL", "price": 5.0, "stock": 1}]
    })
    assert response.status_code == 409


def test_update_product_reorders_image_urls(admin_client: TestClient, db_session):
    product = make_product("REORDER-01")
    product.image_urls = ["url_a", "url_b", "url_c"]
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    response = admin_client.put(
        f"/api/v1/admin/products/{product.id}",
        json={"image_urls": ["url_c", "url_a", "url_b"]},
    )
    assert response.status_code == 200
    assert response.json()["image_urls"] == ["url_c", "url_a", "url_b"]


def test_update_product_omitting_image_urls_preserves_existing(admin_client: TestClient, db_session):
    product = make_product("PRESERVE-IMGS")
    product.image_urls = ["url_a", "url_b"]
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    response = admin_client.put(
        f"/api/v1/admin/products/{product.id}",
        json={"name": "Updated Name"},
    )
    assert response.status_code == 200
    assert response.json()["image_urls"] == ["url_a", "url_b"]


def test_update_product_variant_not_belonging_to_product(admin_client: TestClient, db_session):
    p1 = make_product("P1")
    p1.variants.append(make_variant("P1-VAR"))
    p2 = make_product("P2")
    p2.variants.append(make_variant("P2-VAR"))
    db_session.add_all([p1, p2])
    db_session.commit()
    db_session.refresh(p1)
    db_session.refresh(p2)
    foreign_variant_id = str(p1.variants[0].id)

    response = admin_client.put(f"/api/v1/admin/products/{p2.id}", json={
        "variants": [{"id": foreign_variant_id, "catalog_id": "X", "size_value": 1, "size_unit": "mL", "price": 1.0, "stock": 1}]
    })
    assert response.status_code == 400


# --- DELETE /api/v1/admin/products/{id} ---

def test_delete_product_success(admin_client: TestClient, db_session):
    product = make_product("DEL-01")
    product.variants.append(make_variant("DEL-01-A"))
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    with patch("app.services.product_service.delete_s3_objects_by_urls") as mock_s3:
        response = admin_client.delete(f"/api/v1/admin/products/{product.id}")

    assert response.status_code == 204
    mock_s3.assert_not_called()  # no images → no S3 call


def test_delete_product_cleans_up_s3_images(admin_client: TestClient, db_session):
    product = make_product("DEL-IMGS")
    product.image_urls = [
        "https://cdn.example.com/products/x/1.jpg",
        "https://cdn.example.com/products/x/2.png",
    ]
    product.variants.append(make_variant("DEL-IMGS-A"))
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    with patch("app.services.product_service.delete_s3_objects_by_urls") as mock_s3:
        response = admin_client.delete(f"/api/v1/admin/products/{product.id}")

    assert response.status_code == 204
    mock_s3.assert_called_once_with([
        "https://cdn.example.com/products/x/1.jpg",
        "https://cdn.example.com/products/x/2.png",
    ])


def test_delete_product_s3_failure_still_deletes_from_db(admin_client: TestClient, db_session):
    product = make_product("DEL-S3-FAIL")
    product.image_urls = ["https://cdn.example.com/products/x/1.jpg"]
    product.variants.append(make_variant("DEL-S3-FAIL-A"))
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    product_id = product.id

    with patch("app.services.product_service.delete_s3_objects_by_urls", side_effect=RuntimeError("S3 down")):
        response = admin_client.delete(f"/api/v1/admin/products/{product_id}")

    assert response.status_code == 204

    # Product is gone from DB even though S3 failed
    db_session.expire_all()
    from app.services.product_service import get_product_by_id
    assert get_product_by_id(db_session, product_id) is None


def test_delete_product_not_found(admin_client: TestClient):
    response = admin_client.delete(f"/api/v1/admin/products/{uuid.uuid4()}")
    assert response.status_code == 404


def test_delete_product_invalid_uuid(admin_client: TestClient):
    response = admin_client.delete("/api/v1/admin/products/not-a-uuid")
    assert response.status_code == 422


# --- tag names ---

def test_a_whitespace_only_tag_name_is_rejected(admin_client):
    """min_length=1 was declared before the strip, so "   " passed validation
    and the service's own .strip() then wrote an empty name - a tag nothing can
    match and nothing can usefully display."""
    response = admin_client.post("/api/v1/admin/tags", json={"name": "   "})

    assert response.status_code == 422


def test_a_tag_name_is_stored_trimmed(admin_client, db_session):
    from app.models.tag import Tag

    response = admin_client.post("/api/v1/admin/tags", json={"name": "  Organic  "})

    assert response.status_code == 201
    assert response.json()["name"] == "Organic"
    assert db_session.query(Tag).filter_by(name="Organic").one()


def test_trailing_whitespace_does_not_create_a_second_tag(admin_client):
    """Without the strip these were two different names and both inserted, so
    the tag list showed "Organic" twice."""
    assert admin_client.post("/api/v1/admin/tags", json={"name": "Herbal"}).status_code == 201

    response = admin_client.post("/api/v1/admin/tags", json={"name": "Herbal "})

    assert response.status_code == 409
