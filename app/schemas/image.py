from pydantic import BaseModel, Field


class PresignedUrlRequest(BaseModel):
    extension: str = Field(..., pattern=r"^(jpg|jpeg|png|webp)$")


class PresignedUrlResponse(BaseModel):
    upload_url: str
    image_url: str
    index: int


class ConfirmUploadRequest(BaseModel):
    image_url: str = Field(..., min_length=1)


class DeleteImageRequest(BaseModel):
    image_url: str = Field(..., min_length=1)
