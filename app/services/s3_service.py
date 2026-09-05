from functools import lru_cache
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_IMAGES_PER_PRODUCT = 10


@lru_cache
def _s3_client():
    """Cached: boto3 parses the full S3 service model on every client(), which is
    tens of milliseconds paid on each presign, confirm and delete for no reason.
    The client is thread-safe for the calls made here."""
    settings = get_settings()
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key.get_secret_value(),
        endpoint_url=f"https://s3.{settings.aws_region}.amazonaws.com",
        config=Config(signature_version="s3v4"),
    )


def get_product_url_prefix(product_id: UUID) -> str:
    """Every image URL for a product starts with this. Used to reject URLs that
    were not produced by generate_presigned_upload_url."""
    settings = get_settings()
    return f"{settings.cloudfront_url.rstrip('/')}/products/{product_id}/"


def new_image_key(product_id: UUID, ext: str) -> str:
    """A key no other image for this product can already hold.

    Keys used to be products/{id}/{index}.{ext} with index = len(image_urls) + 1,
    which is only unique while nothing is ever deleted. Delete the second of
    three images and the next upload computes index 3 - a key the third image
    still occupies, and the presigned PUT overwrites it. Two admins uploading at
    once collided the same way. Neither left a trace: the row still lists three
    URLs, one of which now serves the wrong picture.

    A random suffix removes the class of bug rather than narrowing the window.
    Nothing depends on the index: ordering is the position in image_urls (first
    entry is the primary image), and deletion resolves the key from the URL.
    """
    return f"products/{product_id}/{uuid4().hex[:12]}.{ext}"


def key_to_url(key: str) -> str:
    settings = get_settings()
    return f"{settings.cloudfront_url.rstrip('/')}/{key}"


def generate_presigned_upload_url(key: str, ext: str) -> str:
    settings = get_settings()
    content_type = "image/jpeg" if ext in {"jpg", "jpeg"} else f"image/{ext}"

    return _s3_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.s3_bucket_name,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=300,  # 5 minutes
    )


def _url_to_key(image_url: str) -> str | None:
    settings = get_settings()
    prefix = settings.cloudfront_url.rstrip("/") + "/"
    if not image_url.startswith(prefix):
        return None
    return image_url.removeprefix(prefix)


def delete_s3_objects_by_urls(image_urls: list[str]) -> None:
    """Batch-delete all S3 objects for the given CloudFront URLs. Best-effort — raises RuntimeError on failure."""
    if not image_urls:
        return
    settings = get_settings()
    keys = [k for k in (_url_to_key(url) for url in image_urls) if k is not None]
    if not keys:
        return
    try:
        _s3_client().delete_objects(
            Bucket=settings.s3_bucket_name,
            Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
        )
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(f"Failed to delete S3 objects: {e}") from e
