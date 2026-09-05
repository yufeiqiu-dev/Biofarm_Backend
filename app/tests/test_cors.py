"""CORS is the browser-side half of this API's access control.

These assert the middleware is configured no wider than the app actually needs.
The failure they guard against is silent in both directions: too narrow and every
call from the deployed frontend is blocked with a console message the backend
never sees, too wide and any origin the list picks up gets a fully privileged
cross-origin channel.
"""

from fastapi.testclient import TestClient

from app.core.config import get_settings

ALLOWED_ORIGIN = "http://localhost:5174"
FOREIGN_ORIGIN = "https://evil.example.com"


def _preflight(client: TestClient, origin: str, method: str = "POST", headers: str = "authorization"):
    return client.options(
        "/api/v1/products",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_configured_origin_is_allowed(client: TestClient):
    response = _preflight(client, ALLOWED_ORIGIN)
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_unknown_origin_gets_no_allow_header(client: TestClient):
    response = _preflight(client, FOREIGN_ORIGIN)
    assert response.headers.get("access-control-allow-origin") is None


def test_credentials_are_not_allowed(client: TestClient):
    """Auth is a bearer token in a header, never a cookie, so the browser has no
    credentials to send. Advertising the privilege buys nothing and widens what a
    mistake in cors_origins would cost."""
    response = _preflight(client, ALLOWED_ORIGIN)
    assert response.headers.get("access-control-allow-credentials") is None


def test_wildcard_method_and_header_lists_are_gone(client: TestClient):
    response = _preflight(client, ALLOWED_ORIGIN)
    assert response.headers.get("access-control-allow-methods") != "*"
    assert response.headers.get("access-control-allow-headers") != "*"


def test_the_headers_the_frontend_sends_are_allowed(client: TestClient):
    """X-Id-Token is the one the checkout endpoint reads the customer email
    from; dropping it from the list breaks checkout and nothing else."""
    response = _preflight(
        client, ALLOWED_ORIGIN, headers="authorization,content-type,x-id-token"
    )
    allowed = (response.headers.get("access-control-allow-headers") or "").lower()
    for header in ("authorization", "content-type", "x-id-token"):
        assert header in allowed


def test_every_method_the_api_exposes_is_allowed(client: TestClient):
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        response = _preflight(client, ALLOWED_ORIGIN, method=method)
        assert response.status_code == 200, method
        allowed = response.headers.get("access-control-allow-methods") or ""
        assert method in allowed


def test_default_origins_do_not_include_a_wildcard():
    """A wildcard here would pair with any future allow_credentials change to
    open the API to every site the browser visits."""
    assert "*" not in get_settings().cors_origins
