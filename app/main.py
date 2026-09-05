from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings

import app.models  # noqa: F401 - registers ORM models on Base.metadata


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown.

    Deliberately empty. The schema is owned by Alembic and applied with
    `alembic upgrade head` before the app starts; a `create_all` here would be a
    second, silently diverging owner of the same tables. Stale checkout sessions
    are swept by `app.jobs.cleanup` on a schedule rather than on every boot -
    it is daily housekeeping, not startup work, and running it here meant a
    table scan and delete at the exact moment the service was trying to become
    healthy.
    """
    yield

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Authentication is a bearer token in a header, never a cookie, so the
    # browser has no credentials to send cross-origin. Leaving this on asked for
    # a privilege the app does not use and cannot benefit from.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # The only three the frontend sends. X-Id-Token carries the id token the
    # checkout endpoint reads the customer email from.
    allow_headers=["Authorization", "Content-Type", "X-Id-Token"],
)

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
def root():
    return {"message": "Backend is running"}