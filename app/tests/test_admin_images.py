import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.models.product import Product
from app.models.product_variant import ProductVariant


def make_product(cat_id: str, image_urls: list[str] | None = None) -> Product:
    p = Product(cat_id=cat_id, name="Test", description="Test")
    p.image_urls = image_urls or []
    p.variants.append(ProductVariant(catalog_id=f"{cat_id}-V1", size_value=10, size_unit="mL", price=5.0, stock=1))
    return p


FAKE_UPLOAD_URL = "https://s3.amazonaws.com/bucket/presigned"
FAKE_CF_URL = "https://cdn.example.com"


def _patch_s3(presigned_url: str = FAKE_UPLOAD_URL, cf_url: str = FAKE_CF_URL):
    """Context manager that patches s3_service so no real AWS calls happen."""
    mock_generate = MagicMock(return_value=presigned_url)
    mock_delete = MagicMock()
    mock_get_url = MagicMock(side_effect=lambda product_id, index, ext: f"{cf_url}/products/{product_id}/{index}.{ext}")
    return (
        patch("app.api.v1.endpoints.admin_images.generate_presigned_upload_url", mock_generate),
        patch("app.api.v1.endpoints.admin_images.delete_s3_objects_by_urls", mock_delete),
        patch("app.api.v1.endpoints.admin_images.get_image_url", mock_get_url),
    )


# --- POST /presigned-url ---

def test_presigned_url_success(admin_client: TestClient, db_session):
    product = make_product("IMG-01")
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    p1, p2, p3 = _patch_s3()
    with p1, p2, p3:
        response = admin_client.post(
            f"/api/v1/admin/products/{product.id}/images/presigned-url",
            json={"extension": "jpg"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["upload_url"] == FAKE_UPLOAD_URL
    assert data["index"] == 1
    assert "jpg" in data["image_url"]


def test_presigned_url_index_increments(admin_client: TestClient, db_session):
    product = make_product("IMG-02", image_urls=[f"{FAKE_CF_URL}/products/x/1.jpg"])
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    p1, p2, p3 = _patch_s3()
    with p1, p2, p3:
        response = admin_client.post(
            f"/api/v1/admin/products/{product.id}/images/presigned-url",
            json={"extension": "png"},
        )

    assert response.status_code == 200
    assert response.json()["index"] == 2


def test_presigned_url_product_not_found(admin_client: TestClient):
    p1, p2, p3 = _patch_s3()
    with p1, p2, p3:
        response = admin_client.post(
            f"/api/v1/admin/products/{uuid.uuid4()}/images/presigned-url",
            json={"extension": "jpg"},
        )
    assert response.status_code == 404


def test_presigned_url_invalid_extension(admin_client: TestClient, db_session):
    product = make_product("IMG-03")
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    p1, p2, p3 = _patch_s3()
    with p1, p2, p3:
        response = admin_client.post(
            f"/api/v1/admin/products/{product.id}/images/presigned-url",
            json={"extension": "gif"},
        )
    assert response.status_code == 422


def test_presigned_url_max_images_reached(admin_client: TestClient, db_session):
    urls = [f"{FAKE_CF_URL}/products/x/{i}.jpg" for i in range(1, 11)]  # 10 images
    product = make_product("IMG-04", image_urls=urls)
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    p1, p2, p3 = _patch_s3()
    with p1, p2, p3:
        response = admin_client.post(
            f"/api/v1/admin/products/{product.id}/images/presigned-url",
            json={"extension": "jpg"},
        )
    assert response.status_code == 400
    assert "maximum" in response.json()["detail"].lower()


# --- POST /confirm ---

def test_confirm_upload_appends_url(admin_client: TestClient, db_session):
    product = make_product("CONF-01")
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    image_url = f"{FAKE_CF_URL}/products/{product.id}/1.jpg"
    response = admin_client.post(
        f"/api/v1/admin/products/{product.id}/images/confirm",
        json={"image_url": image_url},
    )

    assert response.status_code == 200
    assert image_url in response.json()["image_urls"]


def test_confirm_upload_multiple(admin_client: TestClient, db_session):
    product = make_product("CONF-02")
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    for i in range(1, 4):
        url = f"{FAKE_CF_URL}/products/{product.id}/{i}.jpg"
        response = admin_client.post(
            f"/api/v1/admin/products/{product.id}/images/confirm",
            json={"image_url": url},
        )
        assert response.status_code == 200

    assert len(response.json()["image_urls"]) == 3


def test_confirm_upload_product_not_found(admin_client: TestClient):
    response = admin_client.post(
        f"/api/v1/admin/products/{uuid.uuid4()}/images/confirm",
        json={"image_url": "https://example.com/1.jpg"},
    )
    assert response.status_code == 404


def test_confirm_upload_max_images_reached(admin_client: TestClient, db_session):
    urls = [f"{FAKE_CF_URL}/products/x/{i}.jpg" for i in range(1, 11)]  # 10 images
    product = make_product("CONF-03", image_urls=urls)
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    response = admin_client.post(
        f"/api/v1/admin/products/{product.id}/images/confirm",
        json={"image_url": "https://example.com/11.jpg"},
    )
    assert response.status_code == 400


# --- DELETE /{index} ---

def test_delete_image_success(admin_client: TestClient, db_session):
    product = make_product("DEL-IMG-01", image_urls=[
        f"{FAKE_CF_URL}/products/x/1.jpg",
        f"{FAKE_CF_URL}/products/x/2.png",
    ])
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    p1, p2, p3 = _patch_s3()
    with p1, p2, p3:
        response = admin_client.delete(f"/api/v1/admin/products/{product.id}/images/1")

    assert response.status_code == 204

    # Confirm DB was updated
    db_session.refresh(product)
    assert len(product.image_urls) == 1
    assert "2.png" in product.image_urls[0]


def test_delete_image_index_out_of_range(admin_client: TestClient, db_session):
    product = make_product("DEL-IMG-02", image_urls=[f"{FAKE_CF_URL}/products/x/1.jpg"])
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    p1, p2, p3 = _patch_s3()
    with p1, p2, p3:
        response = admin_client.delete(f"/api/v1/admin/products/{product.id}/images/5")

    assert response.status_code == 404


def test_delete_image_product_not_found(admin_client: TestClient):
    p1, p2, p3 = _patch_s3()
    with p1, p2, p3:
        response = admin_client.delete(f"/api/v1/admin/products/{uuid.uuid4()}/images/1")
    assert response.status_code == 404


def test_delete_image_s3_failure_still_removes_from_db(admin_client: TestClient, db_session):
    product = make_product("DEL-IMG-03", image_urls=[f"{FAKE_CF_URL}/products/x/1.jpg"])
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    mock_delete = MagicMock(side_effect=RuntimeError("S3 unavailable"))
    with patch("app.api.v1.endpoints.admin_images.delete_s3_objects_by_urls", mock_delete):
        response = admin_client.delete(f"/api/v1/admin/products/{product.id}/images/1")

    assert response.status_code == 204
    db_session.refresh(product)
    assert product.image_urls == []
