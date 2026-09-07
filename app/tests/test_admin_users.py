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


# --- finding a customer's orders from the address they actually have ----------
#
# customer_email holds where the customer asked order mail to go, and support
# hears about it precisely when that was wrong. Searching their real address
# would otherwise match nothing, and the admin would have to know to resolve a
# sub by hand first.

def _an_order_for(db_session, sub: str, contact_email: str):
    from app.tests.test_orders import make_order

    order, _ = make_order(db_session, user_id=sub)
    order.customer_email = contact_email
    db_session.commit()
    return order


def test_searching_the_account_address_finds_a_mistyped_order(
    admin_client: TestClient, db_session
):
    """The whole point. The order carries a typo; the customer has the real one."""
    _an_order_for(db_session, SUB, "purchasing@lab.eduu")

    with patch(
        "app.services.cognito_service.get_client",
        return_value=cognito_returning(a_cognito_user()),
    ):
        response = admin_client.get("/api/v1/admin/orders?q=alice@lab.edu")

    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_a_search_still_works_when_cognito_is_down(admin_client: TestClient, db_session):
    """Degraded, not broken. An admin given an error instead of the text matches
    cannot tell "no such customer" from "AWS is down"."""
    _an_order_for(db_session, SUB, "alice@lab.edu")

    client = MagicMock()
    client.exceptions.UserNotFoundException = _UserNotFound
    client.admin_get_user.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "AdminGetUser"
    )
    with patch("app.services.cognito_service.get_client", return_value=client):
        response = admin_client.get("/api/v1/admin/orders?q=alice@lab.edu")

    assert response.status_code == 200
    assert response.json()["total"] == 1, "the text match was lost with the lookup"


def test_a_search_that_is_not_an_address_does_not_call_cognito(
    admin_client: TestClient, db_session
):
    """A name, an order number or a half-typed address costs nothing.

    Deliberately not claiming that no partial address ever resolves - it cannot:
    "alice@lab.co" is both a real address and a prefix of "alice@lab.com". The
    console's 300ms debounce and the negative cache are what bound the calls.
    """
    client = cognito_returning(a_cognito_user())
    with patch("app.services.cognito_service.get_client", return_value=client):
        admin_client.get("/api/v1/admin/orders?q=alice@lab.e")   # one-char TLD
        admin_client.get("/api/v1/admin/orders?q=alice@")
        admin_client.get("/api/v1/admin/orders?q=alice")
        admin_client.get("/api/v1/admin/orders?q=Jane Smith")
        admin_client.get("/api/v1/admin/orders?q=1042")

    assert not client.admin_get_user.called


def test_resolving_does_not_widen_a_search_to_everyone(
    admin_client: TestClient, db_session
):
    """Only the resolved account's orders join the results, not every order."""
    _an_order_for(db_session, SUB, "purchasing@lab.eduu")
    _an_order_for(db_session, "someone-else-sub", "bob@other.org")

    with patch(
        "app.services.cognito_service.get_client",
        return_value=cognito_returning(a_cognito_user()),
    ):
        response = admin_client.get("/api/v1/admin/orders?q=alice@lab.edu")

    assert response.json()["total"] == 1


def test_an_unknown_address_finds_nothing_rather_than_everything(
    admin_client: TestClient, db_session
):
    _an_order_for(db_session, SUB, "purchasing@lab.eduu")

    with patch(
        "app.services.cognito_service.get_client", return_value=cognito_returning(None)
    ):
        response = admin_client.get("/api/v1/admin/orders?q=nobody@nowhere.com")

    assert response.json()["total"] == 0


def test_an_account_with_no_name_resolves_fine(admin_client: TestClient):
    """The shape real accounts actually have.

    The pool asks for email at sign-up and nothing else, so `name` is absent
    from every account in it - checked against the real pool, where the only
    attributes present are email and sub. The fixture above is more generous
    than reality, which is exactly how a missing-attribute crash reaches
    production with the suite green.
    """
    bare = {
        "Username": SUB,
        "Enabled": True,
        "UserStatus": "CONFIRMED",
        "UserCreateDate": datetime(2026, 1, 4, tzinfo=timezone.utc),
        "UserAttributes": [
            {"Name": "sub", "Value": SUB},
            {"Name": "email", "Value": "alice@lab.edu"},
        ],
    }

    with patch("app.services.cognito_service.get_client", return_value=cognito_returning(bare)):
        response = admin_client.get(f"/api/v1/admin/users/{SUB}")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "alice@lab.edu"
    assert body["name"] == "", "a missing name must be empty, not absent or null"


def test_a_plus_addressed_customer_can_be_found(admin_client: TestClient):
    """alice+orders@lab.edu is an ordinary address, and this is the feature that
    has to find those people.

    It passed the endpoint's email check, failed the identifier pattern, and the
    ValueError was swallowed into a text-only search - so the customer whose
    address is least guessable was the one who could never be looked up by it.
    """
    plus = "alice+orders@lab.edu"
    client = cognito_returning(a_cognito_user(email=plus))
    with patch("app.services.cognito_service.get_client", return_value=client):
        response = admin_client.get(f"/api/v1/admin/users/{plus}")

    assert response.status_code == 200, response.json()
    assert client.admin_get_user.call_args.kwargs["Username"] == plus


def test_the_cache_does_not_grow_without_bound(admin_client: TestClient):
    """Its keys are whatever an admin typed that looked like an address, so an
    unbounded dict in a long-lived process never stops growing."""
    client = cognito_returning(None)
    with patch("app.services.cognito_service.get_client", return_value=client):
        for i in range(cognito_service._CACHE_MAX_ENTRIES + 50):
            admin_client.get(f"/api/v1/admin/users/probe{i}@lab.edu")

    assert len(cognito_service._cache) <= cognito_service._CACHE_MAX_ENTRIES
