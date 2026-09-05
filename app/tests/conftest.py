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
        "COGNITO_USER_POOL_CLIENT_ID": "test-app-client-id",
        "AWS_REGION": "us-east-2",
        "S3_BUCKET_NAME": "test-bucket",
        "CLOUDFRONT_URL": "https://test.cloudfront.net",
        "AWS_ACCESS_KEY_ID": "test-key-id",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
        "STRIPE_SECRET_KEY": "sk_test_dummy",
        "STRIPE_WEBHOOK_SECRET": "whsec_test_dummy",
        # Bypassed for the suite at large, so no test can reach SES. Order
        # tests ship and cancel orders, which now send mail, and without this
        # they would each build a real boto3 client and attempt a network call -
        # swallowed by email_service, so the tests would still pass while
        # quietly depending on the network. test_email.py turns it off for
        # itself, which is the only place assertions about sending belong.
        "EMAIL_BYPASS": "true",
        "EMAIL_FROM": "orders@test.invalid",
    }
)

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
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

    # pysqlite ships with legacy transaction handling: it opens transactions
    # implicitly and at the wrong moments, and it will not emit BEGIN for DDL or
    # SAVEPOINT. The consequence here is that SAVEPOINT silently does nothing,
    # which is the whole mechanism db_session relies on to let application code
    # commit and roll back without escaping the test. SQLAlchemy's documented
    # remedy is to take the BEGIN over from the driver.
    @event.listens_for(engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(test_engine):
    """A session whose work is discarded when the test ends.

    join_transaction_mode="create_savepoint" is what makes this honest. Without
    it the session's commit() and rollback() acted on the outer transaction
    directly: a service that rolled back - the order-number retry, any
    IntegrityError path - destroyed everything the test had committed before it,
    so the state an assertion then read was not the state the code produced.
    It was also the source of the "transaction already deassociated" warning
    this fixture used to emit on teardown.

    With a savepoint, commit() and rollback() inside application code behave the
    way they do against a real database, and only this outer rollback undoes it.
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
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
