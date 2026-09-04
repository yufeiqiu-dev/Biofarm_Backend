from functools import lru_cache

import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, Request, status

from app.core.config import get_settings

ADMIN_GROUP = "Admin"

# PyJWKClient has no timeout by default, so a JWKS endpoint that accepts the
# connection and then stalls pins a worker thread for as long as the socket stays
# open. Cognito answers this in milliseconds; five seconds is generous.
JWKS_TIMEOUT_SECONDS = 5


@lru_cache
def _jwks_client() -> PyJWKClient:
    settings = get_settings()
    jwks_url = (
        f"https://cognito-idp.{settings.cognito_region}.amazonaws.com"
        f"/{settings.cognito_user_pool_id}/.well-known/jwks.json"
    )
    return PyJWKClient(jwks_url, cache_keys=True, timeout=JWKS_TIMEOUT_SECONDS)


def _verify_access_token(token: str) -> dict:
    """Validate a Cognito access token and return its claims.

    Both dependencies below go through here. They used to hold their own copies
    of these checks, identical apart from the trailing group assertion, which is
    how a check tightened on one path stays loose on the other.
    """
    settings = get_settings()

    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            # A Cognito *access* token carries no `aud` claim at all - the app
            # client it was issued to lives in `client_id`, checked explicitly
            # below. Leaving audience verification on would reject every token.
            options={"verify_aud": False},
        )
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

    # Bind the token to this application's app client. A valid signature and a
    # matching issuer only prove the token came from our user pool - not that it
    # was minted for us. Without this, an access token issued to any other client
    # in the same pool is accepted, including one added later for an unrelated
    # service with different scopes.
    #
    # Skipped when the id is unset so a local .env without it still works; the
    # production guard in Settings requires it outside development.
    expected_client_id = settings.cognito_user_pool_client_id
    if expected_client_id and payload.get("client_id") != expected_client_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token was not issued for this application",
        )

    return payload


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header.split(" ", 1)[1]


def _dev_identity(groups: list[str]) -> dict:
    return {"sub": "local-dev-user", "email": "dev@example.com", "cognito:groups": groups}


def _missing_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or malformed Authorization header",
    )


def require_user(request: Request) -> dict:
    """Accept any valid Cognito access token, whatever groups it carries.

    A token that is present is always fully verified, auth_bypass or not - the
    bypass only covers the case of no token at all.
    """
    token = _bearer_token(request)
    if token is not None:
        return _verify_access_token(token)

    if get_settings().auth_bypass:
        return _dev_identity([])

    raise _missing_token()


def require_admin(request: Request) -> dict:
    """As require_user, and additionally require membership of the Admin group."""
    token = _bearer_token(request)
    if token is None:
        if get_settings().auth_bypass:
            return _dev_identity([ADMIN_GROUP])
        raise _missing_token()

    payload = _verify_access_token(token)

    # `or []` rather than a .get default: Cognito omits the claim for a user in
    # no groups, but a null claim would make `in` raise and turn a 403 into a 500.
    groups = payload.get("cognito:groups") or []
    if ADMIN_GROUP not in groups:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin group membership required",
        )

    return payload
