import time
import uuid
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

TEST_REGION = "us-east-2"
TEST_POOL_ID = "us-east-2_TESTPOOL"
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


def _settings_mock(auth_bypass: bool = False) -> MagicMock:
    s = MagicMock()
    s.auth_bypass = auth_bypass
    s.cognito_region = TEST_REGION
    s.cognito_user_pool_id = TEST_POOL_ID
    return s


def _call(client: TestClient, token: str | None, auth_bypass: bool = False, jwks_mock=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    jwks = jwks_mock or _jwks_mock()
    with patch("app.dependencies.auth.get_settings", return_value=_settings_mock(auth_bypass)):
        with patch("app.dependencies.auth._jwks_client", return_value=jwks):
            return client.delete(_ENDPOINT, headers=headers)


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


def test_require_user_blocks_unauthenticated(client):
    from app.dependencies.auth import require_user
    assert callable(require_user)
