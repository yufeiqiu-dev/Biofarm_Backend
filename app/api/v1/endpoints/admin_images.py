from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_admin
from app.schemas.image import ConfirmUploadRequest, PresignedUrlRequest, PresignedUrlResponse
from app.services.product_service import get_product_by_id
from app.services.s3_service import (
    MAX_IMAGES_PER_PRODUCT,
    delete_s3_objects_by_urls,
    generate_presigned_upload_url,
    get_image_url,
)

router = APIRouter(
    prefix="/admin/products/{product_id}/images",
    tags=["admin-images"],
)


def _get_product_or_404(db: Session, product_id: UUID):
    product = get_product_by_id(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


@router.post("/presigned-url", response_model=PresignedUrlResponse, status_code=status.HTTP_200_OK)
def get_presigned_url(
    product_id: UUID,
    payload: PresignedUrlRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    product = _get_product_or_404(db, product_id)

    current_count = len(product.image_urls or [])
    if current_count >= MAX_IMAGES_PER_PRODUCT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product already has the maximum of {MAX_IMAGES_PER_PRODUCT} images",
        )

    ext = payload.extension.lower()
    next_index = current_count + 1
    upload_url = generate_presigned_upload_url(product_id, next_index, ext)
    image_url = get_image_url(product_id, next_index, ext)

    return PresignedUrlResponse(upload_url=upload_url, image_url=image_url, index=next_index)


@router.post("/confirm", status_code=status.HTTP_200_OK)
def confirm_upload(
    product_id: UUID,
    payload: ConfirmUploadRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    product = _get_product_or_404(db, product_id)

    current_urls = list(product.image_urls or [])
    if len(current_urls) >= MAX_IMAGES_PER_PRODUCT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product already has the maximum of {MAX_IMAGES_PER_PRODUCT} images",
        )

    current_urls.append(payload.image_url)
    product.image_urls = current_urls
    db.commit()

    return {"image_urls": current_urls}


@router.delete("/{index}", status_code=status.HTTP_204_NO_CONTENT)
def delete_image(
    product_id: UUID,
    index: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    product = _get_product_or_404(db, product_id)

    current_urls = list(product.image_urls or [])
    if index < 1 or index > len(current_urls):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image at index {index} not found",
        )

    url = current_urls[index - 1]

    try:
        delete_s3_objects_by_urls([url])
    except RuntimeError:
        # S3 deletion failed — still remove from DB so the list stays consistent
        pass

    current_urls.pop(index - 1)
    product.image_urls = current_urls
    db.commit()
