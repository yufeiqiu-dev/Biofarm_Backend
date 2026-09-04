import os

# Pin the entire configuration before anything imports app.core.config.
#
# Settings reads Biofarm_Backend/.env, so without this the suite inherits
# whatever the developer happens to have set locally: AUTH_BYPASS=true silently
# turns every "unauthenticated request is rejected" test green-to-red, and
# STRIPE_BYPASS=true changes which code path creates an order. Tests that pass
# or fail based on an untracked file are not tests. Real environment variables
# take precedence over the .env file in pydantic-settings, so setting them here
# makes the suite deterministic and independent of the machine it runs on.
#
# This must stay above the app imports below - get_settings() is lru_cached, and
# the first import wins.
os.environ.update(
    {
        "APP_ENV": "test",
        "AUTH_BYPASS": "false",
        "STRIPE_BYPASS": "false",
        "DATABASE_URL": "sqlite:///:memory:",
        "COGNITO_REGION": "us-east-2",
        "COGNITO_USER_POOL_ID": "us-east-2_test",
        "AWS_REGION": "us-east-2",
        "S3_BUCKET_NAME": "test-bucket",
        "CLOUDFRONT_URL": "https://test.cloudfront.net",
        "AWS_ACCESS_KEY_ID": "test-key-id",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
        "STRIPE_SECRET_KEY": "sk_test_dummy",
        "STRIPE_WEBHOOK_SECRET": "whsec_test_dummy",
    }
)

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.models  # noqa: F401, E402 — registers models on Base.metadata
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.dependencies.auth import require_admin, require_user  # noqa: E402
from app.main import app  # noqa: E402

TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture(scope="session")
def test_engine():
    engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(test_engine):
    connection = test_engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(db_session):
    def override_get_db():
        yield db_session

    def override_require_admin():
        return {"sub": "test-admin", "email": "admin@test.com", "cognito:groups": ["Admin"]}

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = override_require_admin

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def user_client(db_session):
    def override_get_db():
        yield db_session

    def override_require_user():
        return {"sub": "test-user-123", "email": "user@test.com", "cognito:groups": []}

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_user] = override_require_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
