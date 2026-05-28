from functools import lru_cache

import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, Request, status

from app.core.config import get_settings


@lru_cache
def _jwks_client() -> PyJWKClient:
    settings = get_settings()
    jwks_url = (
        f"https://cognito-idp.{settings.cognito_region}.amazonaws.com"
        f"/{settings.cognito_user_pool_id}/.well-known/jwks.json"
    )
    return PyJWKClient(jwks_url, cache_keys=True)


def _decode_token(token: str, settings) -> dict:
    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        options={"verify_aud": False},
    )


def require_admin(request: Request):
    settings = get_settings()

    auth_header = request.headers.get("Authorization", "")
    has_token = auth_header.startswith("Bearer ")

    if settings.auth_bypass:
        if has_token:
            # Real token present — decode and return the actual user
            try:
                return _decode_token(auth_header.split(" ", 1)[1], settings)
            except Exception:
                pass
        # No token (e.g. direct curl testing) — fall back to mock
        return {
            "sub": "local-dev-user",
            "email": "dev@example.com",
            "groups": ["Admin"],
        }

    if not has_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )

    token = auth_header.split(" ", 1)[1]

    try:
        payload = _decode_token(token, settings)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        )

    expected_iss = (
        f"https://cognito-idp.{settings.cognito_region}.amazonaws.com"
        f"/{settings.cognito_user_pool_id}"
    )
    if payload.get("iss") != expected_iss:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token issuer",
        )

    if payload.get("token_use") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Expected an access token",
        )

    groups = payload.get("cognito:groups", [])
    if "Admin" not in groups:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin group membership required",
        )

    return payload
