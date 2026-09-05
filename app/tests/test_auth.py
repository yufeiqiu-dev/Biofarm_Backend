import time
import uuid
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

TEST_REGION = "us-east-2"
TEST_POOL_ID = "us-east-2_TESTPOOL"
TEST_CLIENT_ID = "test-app-client-id"
TEST_ISS = f"https://cognito-idp.{TEST_REGION}.amazonaws.com/{TEST_POOL_ID}"

# RSA key pair for signing test tokens — generated once per module
_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()
_wrong_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Use DELETE on a random product ID: 404 = auth passed, 401/403 = auth failed
_ENDPOINT = f"/api/v1/admin/products/{uuid.uuid4()}"


def _make_token(**overrides) -> str:
    now = int(time.time())
    payload = {
        "sub": "user-123",
        "iss": TEST_ISS,
        "token_use": "access",
        "client_id": TEST_CLIENT_ID,
        "cognito:groups": ["Admin"],
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, _private_key, algorithm="RS256")


def _jwks_mock():
    signing_key = MagicMock()
    signing_key.key = _public_key
    mock = MagicMock()
    mock.get_signing_key_from_jwt.return_value = signing_key
    return mock


def _settings_mock(auth_bypass: bool = False, client_id: str = TEST_CLIENT_ID) -> MagicMock:
    s = MagicMock()
    s.auth_bypass = auth_bypass
    s.cognito_region = TEST_REGION
    s.cognito_user_pool_id = TEST_POOL_ID
    # Set explicitly: a bare MagicMock attribute is truthy and would compare
    # unequal to every real client_id, failing every token in the suite.
    s.cognito_user_pool_client_id = client_id
    return s


def _call(
    client: TestClient,
    token: str | None,
    auth_bypass: bool = False,
    jwks_mock=None,
    client_id: str = TEST_CLIENT_ID,
    endpoint: str = _ENDPOINT,
    method: str = "delete",
):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    jwks = jwks_mock or _jwks_mock()
    settings = _settings_mock(auth_bypass, client_id)
    with patch("app.dependencies.auth.get_settings", return_value=settings):
        with patch("app.dependencies.auth._jwks_client", return_value=jwks):
            return getattr(client, method)(endpoint, headers=headers)


# --- auth_bypass=False ---

def test_missing_token_returns_401(client: TestClient):
    response = _call(client, token=None)
    assert response.status_code == 401
    assert "Missing" in response.json()["detail"]


def test_valid_admin_token_passes_auth(client: TestClient):
    response = _call(client, token=_make_token())
    # 404 means auth passed; product simply doesn't exist
    assert response.status_code == 404


def test_expired_token_returns_401(client: TestClient):
    token = _make_token(exp=int(time.time()) - 3600)
    response = _call(client, token=token)
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_wrong_signature_returns_401(client: TestClient):
    # Signed with a different key; mock returns the correct public key so decode fails
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "x",
            "iss": TEST_ISS,
            "token_use": "access",
            "cognito:groups": ["Admin"],
            "iat": now,
            "exp": now + 3600,
        },
        _wrong_private_key,
        algorithm="RS256",
    )
    response = _call(client, token=token)
    assert response.status_code == 401


def test_malformed_token_returns_401(client: TestClient):
    jwks = _jwks_mock()
    jwks.get_signing_key_from_jwt.side_effect = jwt.DecodeError("bad token")
    response = _call(client, token="not-a-jwt", jwks_mock=jwks)
    assert response.status_code == 401


def test_wrong_issuer_returns_401(client: TestClient):
    token = _make_token(iss="https://cognito-idp.us-west-2.amazonaws.com/wrong_pool")
    response = _call(client, token=token)
    assert response.status_code == 401
    assert "issuer" in response.json()["detail"].lower()


def test_id_token_instead_of_access_token_returns_401(client: TestClient):
    token = _make_token(token_use="id")
    response = _call(client, token=token)
    assert response.status_code == 401
    assert "access token" in response.json()["detail"].lower()


def test_non_admin_group_returns_403(client: TestClient):
    token = _make_token(**{"cognito:groups": ["Users"]})
    response = _call(client, token=token)
    assert response.status_code == 403
    assert "Admin" in response.json()["detail"]


def test_no_groups_returns_403(client: TestClient):
    token = _make_token(**{"cognito:groups": []})
    response = _call(client, token=token)
    assert response.status_code == 403


# --- auth_bypass=True ---

def test_bypass_no_token_uses_mock_identity(client: TestClient):
    # bypass=True with no token → mock admin identity, auth passes
    with patch("app.dependencies.auth.get_settings", return_value=_settings_mock(auth_bypass=True)):
        response = client.delete(_ENDPOINT)
    assert response.status_code == 404


def test_bypass_valid_token_still_validates(client: TestClient):
    # bypass=True with a real valid token → validates normally and passes
    response = _call(client, token=_make_token(), auth_bypass=True)
    assert response.status_code == 404


def test_bypass_expired_token_still_rejected(client: TestClient):
    # bypass=True but an expired token was provided → should still reject
    token = _make_token(exp=int(time.time()) - 3600)
    response = _call(client, token=token, auth_bypass=True)
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_bypass_non_admin_token_still_rejected(client: TestClient):
    # bypass=True but token lacks Admin group → still rejects
    token = _make_token(**{"cognito:groups": ["Users"]})
    response = _call(client, token=token, auth_bypass=True)
    assert response.status_code == 403


# --- app client binding (client_id) ---

def test_token_from_another_app_client_returns_401(client: TestClient):
    """A token minted by a different app client in the same user pool has a
    valid signature and the right issuer, and must still be rejected."""
    token = _make_token(client_id="some-other-app-client")
    response = _call(client, token=token)
    assert response.status_code == 401
    assert "not issued for this application" in response.json()["detail"]


def test_token_with_no_client_id_claim_returns_401(client: TestClient):
    token = _make_token()
    payload = jwt.decode(token, options={"verify_signature": False})
    del payload["client_id"]
    token = jwt.encode(payload, _private_key, algorithm="RS256")
    response = _call(client, token=token)
    assert response.status_code == 401


def test_client_id_check_skipped_when_unconfigured(client: TestClient):
    """An existing .env without COGNITO_USER_POOL_CLIENT_ID must keep working;
    the production guard is what makes it mandatory outside development."""
    token = _make_token(client_id="anything-at-all")
    response = _call(client, token=token, client_id="")
    assert response.status_code == 404


def test_bypass_wrong_client_id_still_rejected(client: TestClient):
    token = _make_token(client_id="some-other-app-client")
    response = _call(client, token=token, auth_bypass=True)
    assert response.status_code == 401


# --- require_user shares the same verification ---

_USER_ENDPOINT = "/api/v1/orders"


def test_require_user_rejects_token_from_another_app_client(client: TestClient):
    """require_user and require_admin used to carry separate copies of these
    checks. This asserts they now share one, so a check added to the admin path
    cannot go missing from the customer path."""
    token = _make_token(client_id="some-other-app-client")
    response = _call(client, token=token, endpoint=_USER_ENDPOINT, method="get")
    assert response.status_code == 401


def test_require_user_rejects_id_token(client: TestClient):
    token = _make_token(token_use="id")
    response = _call(client, token=token, endpoint=_USER_ENDPOINT, method="get")
    assert response.status_code == 401


def test_require_user_rejects_wrong_issuer(client: TestClient):
    token = _make_token(iss="https://cognito-idp.us-west-2.amazonaws.com/wrong_pool")
    response = _call(client, token=token, endpoint=_USER_ENDPOINT, method="get")
    assert response.status_code == 401


def test_require_user_accepts_a_non_admin_token(client: TestClient):
    """No group membership required - the admin path's 403 must not leak here."""
    token = _make_token(**{"cognito:groups": []})
    response = _call(client, token=token, endpoint=_USER_ENDPOINT, method="get")
    assert response.status_code == 200


def test_require_user_missing_token_returns_401(client: TestClient):
    response = _call(client, token=None, endpoint=_USER_ENDPOINT, method="get")
    assert response.status_code == 401


def test_null_groups_claim_returns_403_not_500(client: TestClient):
    """A null cognito:groups claim used to reach `"Admin" in None`."""
    token = _make_token(**{"cognito:groups": None})
    response = _call(client, token=token)
    assert response.status_code == 403


# --- the id token behind X-Id-Token ---
#
# An access token carries no email claim, so checkout reads it from the id token
# the frontend forwards. That value used to be decoded with the signature check
# disabled - "only a hint, never an authorization decision". True of how it is
# used, and beside the point: the email is written onto the order and shown in
# the admin console, so an unverified claim let any signed-in customer put an
# arbitrary address into the record fulfilment works from.

def _id_token(**overrides) -> str:
    now = int(time.time())
    payload = {
        "sub": "user-123",
        "iss": TEST_ISS,
        "token_use": "id",
        "aud": TEST_CLIENT_ID,
        "email": "real@example.com",
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, _private_key, algorithm="RS256")


def _verify(token: str, client_id: str = TEST_CLIENT_ID):
    from app.dependencies.auth import verify_id_token

    with patch("app.dependencies.auth.get_settings", return_value=_settings_mock(client_id=client_id)):
        with patch("app.dependencies.auth._jwks_client", return_value=_jwks_mock()):
            return verify_id_token(token)


def test_a_properly_signed_id_token_is_accepted():
    claims = _verify(_id_token())
    assert claims is not None
    assert claims["email"] == "real@example.com"


def test_an_unsigned_token_is_rejected():
    """The exact forgery this closes: a customer crafts a token with their own
    sub and any email they like, and signs it with nothing."""
    forged = jwt.encode(
        {
            "sub": "user-123",
            "iss": TEST_ISS,
            "token_use": "id",
            "aud": TEST_CLIENT_ID,
            "email": "someone.else@example.com",
            "exp": int(time.time()) + 3600,
        },
        "a-key-long-enough-to-not-warn-about-hmac-length",
        algorithm="HS256",
    )
    assert _verify(forged) is None


def test_a_token_signed_with_the_wrong_key_is_rejected():
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "user-123",
            "iss": TEST_ISS,
            "token_use": "id",
            "aud": TEST_CLIENT_ID,
            "email": "attacker@example.com",
            "exp": now + 3600,
        },
        _wrong_private_key,
        algorithm="RS256",
    )
    assert _verify(token) is None


def test_an_access_token_is_not_accepted_as_an_id_token():
    """It is signed by the same pool and would pass every other check."""
    assert _verify(_id_token(token_use="access")) is None


def test_an_id_token_for_another_app_client_is_rejected():
    """Unlike an access token, an id token carries `aud` - so this is the claim
    that binds it to us."""
    assert _verify(_id_token(aud="some-other-app-client")) is None


def test_an_expired_id_token_is_rejected():
    assert _verify(_id_token(exp=int(time.time()) - 3600)) is None


def test_an_id_token_from_another_pool_is_rejected():
    assert _verify(_id_token(iss="https://cognito-idp.us-west-2.amazonaws.com/other")) is None


def test_a_malformed_token_returns_none_rather_than_raising():
    """Checkout must not fail because the email could not be established."""
    assert _verify("not-a-jwt-at-all") is None
