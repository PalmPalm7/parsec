"""The alert endpoint must authenticate before it validates the request body.

Found on a live deployment: the e2e API check posted to
/api/alert/investigate without a key and got a 422 listing the missing body
fields, not a 401. The key was compared inside the handler, and FastAPI
validates the body before the handler runs, so an unauthenticated caller
learned the request schema — and the harness counted the 422 as a pass.
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

KEY = "secret-key-123"
BAD_BODY = {"account_name": "missing every required field"}


class _Cfg:
    def __init__(self, key: str) -> None:
        self._key = key

    def get(self, name, default=""):
        return self._key if name == "alert_api_key" else default


@pytest.fixture()
def client(monkeypatch):
    from src.app import app

    @asynccontextmanager
    async def _noop_lifespan(app_):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan)
    monkeypatch.setattr("src.routes.alert.run_alert_investigation", AsyncMock())
    return TestClient(app, raise_server_exceptions=False)


def _configure(monkeypatch, key: str) -> None:
    monkeypatch.setattr("src.routes.alert.get_config", lambda: _Cfg(key))


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "wrong-key"}],
    ids=["no-key", "wrong-key"],
)
@pytest.mark.parametrize("body", [BAD_BODY, None], ids=["invalid-body", "no-body"])
def test_unauthenticated_caller_gets_401_not_the_schema(client, monkeypatch, headers, body):
    _configure(monkeypatch, KEY)
    resp = client.post("/api/alert/investigate", json=body, headers=headers)

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or missing API key"}


def test_unconfigured_endpoint_is_503_before_body_validation(client, monkeypatch):
    _configure(monkeypatch, "")
    resp = client.post("/api/alert/investigate", json=BAD_BODY)

    assert resp.status_code == 503


def test_authenticated_caller_still_gets_body_validation(client, monkeypatch):
    _configure(monkeypatch, KEY)
    resp = client.post("/api/alert/investigate", json=BAD_BODY, headers={"X-API-Key": KEY})

    assert resp.status_code == 422


def test_key_is_compared_in_constant_time(client, monkeypatch):
    _configure(monkeypatch, KEY)
    calls: list[tuple] = []
    real = hmac.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("src.routes.alert.hmac.compare_digest", _spy)
    resp = client.post("/api/alert/investigate", json=BAD_BODY, headers={"X-API-Key": "nope"})

    assert resp.status_code == 401
    assert calls == [(b"nope", KEY.encode())]


def test_non_ascii_key_is_a_401_not_a_500(client, monkeypatch):
    """compare_digest raises TypeError on non-ASCII str; the dependency compares bytes."""
    _configure(monkeypatch, KEY)
    resp = client.post(
        "/api/alert/investigate",
        json=BAD_BODY,
        headers={"X-API-Key": "clé".encode("latin-1")},
    )

    assert resp.status_code == 401
