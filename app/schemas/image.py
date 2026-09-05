from pydantic import BaseModel, Field

from app.services.s3_service import ALLOWED_EXTENSIONS

# Built from the one list rather than repeated as a literal, so adding a format
# cannot leave the validator and the content-type mapping disagreeing.
_EXTENSION_PATTERN = rf"^({'|'.join(sorted(ALLOWED_EXTENSIONS))})$"


class PresignedUrlRequest(BaseModel):
    extension: str = Field(..., pattern=_EXTENSION_PATTERN)


class PresignedUrlResponse(BaseModel):
    upload_url: str
    image_url: str


class ConfirmUploadRequest(BaseModel):
    image_url: str = Field(..., min_length=1)


class DeleteImageRequest(BaseModel):
    image_url: str = Field(..., min_length=1)
