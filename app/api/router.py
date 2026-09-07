from fastapi import APIRouter

from app.api.v1.endpoints.health import router as health_router
from app.api.v1.endpoints.admin_products import router as admin_products_router
from app.api.v1.endpoints.admin_images import router as admin_images_router
from app.api.v1.endpoints.admin_tags import router as admin_tags_router
from app.api.v1.endpoints.admin_orders import router as admin_orders_router
from app.api.v1.endpoints.admin_stats import router as admin_stats_router
from app.api.v1.endpoints.admin_users import router as admin_users_router
from app.api.v1.endpoints.orders import router as orders_router
from app.api.v1.endpoints.products import router as products_router
from app.api.v1.endpoints.tags import router as tags_router
from app.api.v1.endpoints.stripe_webhook import router as stripe_webhook_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(products_router)
api_router.include_router(tags_router)
api_router.include_router(admin_products_router)
api_router.include_router(admin_images_router)
api_router.include_router(admin_tags_router)
api_router.include_router(orders_router)
api_router.include_router(stripe_webhook_router)
api_router.include_router(admin_orders_router)
api_router.include_router(admin_stats_router)
api_router.include_router(admin_users_router)