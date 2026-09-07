"""The admin dashboard's figures.

One request rather than several, because the page shows them together and a
dashboard that paints in four stages reads as broken.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_admin
from app.schemas.stats import DashboardStatsOut
from app.services.stats_service import get_dashboard_stats

router = APIRouter(prefix="/admin/stats", tags=["admin-stats"])


@router.get("", response_model=DashboardStatsOut)
def get_stats(db: Session = Depends(get_db), _=Depends(require_admin)):
    return get_dashboard_stats(db)
