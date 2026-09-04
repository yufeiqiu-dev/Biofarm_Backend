from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check():
    """Liveness: is the process up and serving?

    Deliberately touches nothing. A liveness probe that depends on the database
    turns a database blip into a restart loop, which makes the outage worse.
    """
    return {"status": "ok"}


@router.get("/health/ready")
def readiness_check(db: Session = Depends(get_db)):
    """Readiness: can this instance actually serve requests?

    Point the load balancer's health check here rather than at /health. Every
    useful endpoint needs the database, so an instance that cannot reach it
    should stop receiving traffic instead of answering "ok" and then failing
    each request.

    The query is deliberately trivial - this runs continuously.
    """
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"database unreachable: {type(exc).__name__}",
        )
    return {"status": "ok", "database": "ok"}
