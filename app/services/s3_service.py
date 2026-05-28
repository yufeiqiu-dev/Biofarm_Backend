from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_IMAGES_PER_PRODUCT = 10


def _s3_client():
    settings = get_settings()
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        endpoint_url=f"https://s3.{settings.aws_region}.amazonaws.com",
        config=Config(signature_version="s3v4"),
    )


def get_image_key(product_id: UUID, index: int, ext: str) -> str:
    return f"products/{product_id}/{index}.{ext}"


def get_image_url(product_id: UUID, index: int, ext: str) -> str:
    settings = get_settings()
    key = get_image_key(product_id, index, ext)
    return f"{settings.cloudfront_url.rstrip('/')}/{key}"


def generate_presigned_upload_url(product_id: UUID, index: int, ext: str) -> str:
    settings = get_settings()
    key = get_image_key(product_id, index, ext)
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


def delete_s3_object(product_id: UUID, index: int, ext: str) -> None:
    settings = get_settings()
    key = get_image_key(product_id, index, ext)
    try:
        _s3_client().delete_object(Bucket=settings.s3_bucket_name, Key=key)
    except (ClientError, BotoCoreError) as e:
        raise RuntimeError(f"Failed to delete S3 object: {e}") from e


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
