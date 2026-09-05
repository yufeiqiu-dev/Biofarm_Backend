from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings

settings = get_settings()

_engine_kwargs: dict = {
    # Echo logs every statement *and its bound parameters*. Against a deployed
    # database those parameters are customer emails, names, phone numbers and
    # shipping addresses, so this must stay off outside local development.
    "echo": settings.app_env.lower() == "dev",
}

# SQLite (used by the test suite) is served by SingletonThreadPool, which rejects
# the sizing arguments below, so these apply only to a real server-backed pool.
if not settings.database_url.startswith("sqlite"):
    _engine_kwargs.update(
        # Against localhost a connection lives as long as the process. Against
        # RDS reached through a NAT, idle connections get dropped by the
        # database's own timeout and by NAT idle timeouts, and the pool hands
        # the application a dead one. Without pre_ping that surfaces as an
        # intermittent OperationalError on the first request after a quiet
        # period - overnight, and never reproducible on demand.
        pool_pre_ping=True,
        pool_recycle=1800,
        # Kept small on purpose: db.t4g.micro allows on the order of 80
        # connections in total, shared with migrations, jobs and any other client.
        pool_size=5,
        max_overflow=5,
    )

engine = create_engine(settings.database_url, **_engine_kwargs)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()