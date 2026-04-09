from fastapi import APIRouter, Depends

from app.dependencies.auth import require_admin

router = APIRouter(
    prefix="/admin/products",
    tags=["admin-products"],
)


@router.get("/ping")
def admin_products_ping(current_user=Depends(require_admin)):
    return {
        "message": "Admin products router ready",
        "current_user": current_user,
    }