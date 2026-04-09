from fastapi import APIRouter

from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.admin_products import router as admin_products_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(admin_products_router)