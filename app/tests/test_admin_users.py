"""Resolving an order's user_id to the Cognito account behind it.

Cognito is never called for real here - the client is patched. What is worth
asserting is the shape of the questions asked of it, and the difference between
an account that is absent and a lookup that could not be made.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.services import cognito_service

SUB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _no_carry_over():
    """The cache is module-level, so one test's answer would be another's."""
    cognito_service.clear_cache()
    yield
    cognito_service.clear_cache()


def a_cognito_user(sub: str = SUB, email: str = "alice@lab.edu"):
    """The AdminGetUser shape - UserAttributes, not ListUsers' Attributes."""
    return {
        "Username": sub,
        "Enabled": True,
        "UserStatus": "CONFIRMED",
        "UserCreateDate": datetime(2026, 1, 4, tzinfo=timezone.utc),
        "UserAttributes": [
            {"Name": "sub", "Value": sub},
            {"Name": "email", "Value": email},
            {"Name": "name", "Value": "Alice Chen"},
        ],
    }


class _UserNotFound(Exception):
    """Stands in for botocore's generated UserNotFoundException."""


def cognito_returning(user):
    """A client that answers admin_get_user, or reports the account is gone."""
    client = MagicMock()
    client.exceptions.UserNotFoundException = _UserNotFound
    if user is None:
        client.admin_get_user.side_effect = _UserNotFound()
    else:
        client.admin_get_user.return_value = user
    return client


def test_resolves_a_sub_to_the_account(admin_client: TestClient):
    with patch("app.services.cognito_service.get_client", return_value=cognito_returning(a_cognito_user())):
        response = admin_client.get(f"/api/v1/admin/users/{SUB}")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "alice@lab.edu"
    assert body["name"] == "Alice Chen"
    assert body["status"] == "CONFIRMED"
    assert body["enabled"] is True


def test_asks_for_one_account_rather_than_the_whole_pool(admin_client: TestClient):
    """AdminGetUser, not ListUsers.

    Both need the same IAM resource - neither scopes below the pool - but
    ListUsers would let whatever holds the credential enumerate every customer,
    and this reads only an account whose sub the caller already has.
    """
    client = cognito_returning(a_cognito_user())
    with patch("app.services.cognito_service.get_client", return_value=client):
        admin_client.get(f"/api/v1/admin/users/{SUB}")

    assert not client.list_users.called, "the whole pool was enumerated"
    kwargs = client.admin_get_user.call_args.kwargs
    assert kwargs["Username"] == SUB
    assert kwargs["UserPoolId"]


def test_a_deleted_account_is_404_not_an_error(admin_client: TestClient):
    """Orders outlive accounts, which is normal rather than broken."""
    with patch("app.services.cognito_service.get_client", return_value=cognito_returning(None)):
        response = admin_client.get(f"/api/v1/admin/users/{SUB}")

    assert response.status_code == 404


def test_an_unreachable_cognito_is_not_reported_as_absence(admin_client: TestClient):
    """502, not 404. Presenting a failed lookup as "no such customer" would make
    every customer look deleted the moment Cognito was unreachable."""
    client = MagicMock()
    client.exceptions.UserNotFoundException = _UserNotFound
    client.admin_get_user.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        "AdminGetUser",
    )
    with patch("app.services.cognito_service.get_client", return_value=client):
        response = admin_client.get(f"/api/v1/admin/users/{SUB}")

    assert response.status_code == 502


def test_obvious_junk_never_reaches_cognito(admin_client: TestClient):
    """The sub arrives from a URL path.

    Under the earlier ListUsers version this was load-bearing - the sub went into
    a filter expression, where a quote changed its meaning. AdminGetUser takes it
    as a parameter, so this is now only about not paying for a round trip to
    learn that "x\" or sub ^= \"" is not a user id.
    """
    client = cognito_returning(a_cognito_user())
    with patch("app.services.cognito_service.get_client", return_value=client):
        response = admin_client.get('/api/v1/admin/users/x" or sub ^= "')

    assert response.status_code in (400, 404)
    assert not client.admin_get_user.called, "a malformed sub reached Cognito"


def test_a_repeat_lookup_does_not_call_cognito_again(admin_client: TestClient):
    client = cognito_returning(a_cognito_user())
    with patch("app.services.cognito_service.get_client", return_value=client):
        admin_client.get(f"/api/v1/admin/users/{SUB}")
        admin_client.get(f"/api/v1/admin/users/{SUB}")

    assert client.admin_get_user.call_count == 1


def test_a_missing_account_is_cached_too(admin_client: TestClient):
    """Otherwise every order of a deleted customer is a fresh call that will
    never start succeeding."""
    client = cognito_returning(None)
    with patch("app.services.cognito_service.get_client", return_value=client):
        admin_client.get(f"/api/v1/admin/users/{SUB}")
        admin_client.get(f"/api/v1/admin/users/{SUB}")

    assert client.admin_get_user.call_count == 1


def test_the_bypass_identity_never_reaches_cognito(admin_client: TestClient, monkeypatch):
    """AUTH_BYPASS invents a sub that no pool contains. Resolving it would be a
    call that always fails, and would make local development need an IAM
    permission it otherwise does not."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_bypass", True, raising=False)

    client = cognito_returning(None)
    with patch("app.services.cognito_service.get_client", return_value=client):
        response = admin_client.get("/api/v1/admin/users/local-dev-user")

    assert response.status_code == 200
    assert response.json()["synthetic"] is True
    assert not client.admin_get_user.called


def test_only_an_admin_may_ask(client: TestClient):
    """An anonymous caller must not be able to enumerate accounts by id."""
    response = client.get(f"/api/v1/admin/users/{SUB}")
    assert response.status_code in (401, 403)
