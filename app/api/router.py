from fastapi import APIRouter

from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.admin_products import router as admin_products_router
from app.api.v1.endpoints.admin_images import router as admin_images_router
from app.api.v1.endpoints.products import router as products_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(products_router)
api_router.include_router(admin_products_router)
api_router.include_router(admin_images_router)