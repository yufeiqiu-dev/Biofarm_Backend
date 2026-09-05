"""Liveness and readiness.

The distinction is load-bearing for deployment: the platform's health check
points at readiness, so an instance that cannot reach the database stops
receiving traffic instead of answering "ok" and then failing every request.
Liveness stays shallow on purpose - a liveness probe that depends on the
database turns a database blip into a restart loop.
"""

from unittest.mock import patch

from sqlalchemy.exc import OperationalError


def test_liveness_is_ok(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_ok_when_database_answers(client):
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_readiness_fails_when_database_is_unreachable(client):
    """A dead database must make this instance unready, not "healthy"."""
    with patch(
        "app.api.v1.endpoints.health.text",
        side_effect=OperationalError("SELECT 1", {}, Exception("connection refused")),
    ):
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert "database unreachable" in response.json()["detail"]


def test_liveness_still_ok_when_database_is_unreachable(client):
    """Liveness must not depend on the database - that is the whole point."""
    with patch(
        "app.api.v1.endpoints.health.text",
        side_effect=OperationalError("SELECT 1", {}, Exception("connection refused")),
    ):
        response = client.get("/api/v1/health")

    assert response.status_code == 200
