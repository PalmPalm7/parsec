"""The /api/debug routes must enforce the same allowed-user gate as the rest of the API.

Found on a live deployment: the oauth-proxy admits any cluster login
(``-email-domain=*``, no group check) and leaves group membership to the app,
but the debug router never called ``_check_user_allowed``. A user outside every
allowed group could make Parsec fetch AAP2 job data with its own controller
credentials.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

JOB = "https://aap2-partner0-prod-us-east-2.aap.infra.partner.demo.redhat.com/#/jobs/playbook/1/output"
ENDPOINTS = [
    ("/api/debug/diagnose", {"url": JOB}, "fetch_job_metadata"),
    ("/api/debug/correlation", {"url": JOB, "job_id": 1}, "fetch_correlation"),
    ("/api/debug/ee", {"url": JOB, "job_id": 1, "ee_id": 1}, "fetch_ee_info"),
]


@pytest.fixture()
def client(monkeypatch):
    from src.app import app

    @asynccontextmanager
    async def _noop_lifespan(app_):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("path", "body", "fetcher"), ENDPOINTS)
def test_denied_user_is_refused_before_any_aap2_call(client, path, body, fetcher):
    denied = AsyncMock(side_effect=HTTPException(status_code=403, detail="Access denied"))
    with (
        patch("src.routes.debug._check_user_allowed", denied),
        patch(f"src.routes.debug.{fetcher}", new_callable=AsyncMock) as fetch,
        patch("src.routes.debug.find_controller_for_url", return_value="partner0"),
    ):
        resp = client.post(path, json=body, headers={"X-Forwarded-Email": "outsider@example.com"})

    assert resp.status_code == 403
    fetch.assert_not_called()
    assert denied.await_args.args[1] == "outsider@example.com"


@pytest.mark.parametrize(("path", "body", "fetcher"), ENDPOINTS)
def test_rejected_controller_credentials_are_a_502(client, path, body, fetcher):
    """Parsec's credential failing is a server fault, not "you are unauthenticated"."""
    with (
        patch("src.routes.debug._check_user_allowed", AsyncMock(return_value=None)),
        patch(
            f"src.routes.debug.{fetcher}",
            AsyncMock(
                side_effect=PermissionError("Authentication failed for controller 'partner0'")
            ),
        ),
        patch("src.routes.debug.find_controller_for_url", return_value="partner0"),
    ):
        resp = client.post(path, json=body, headers={"X-Forwarded-Email": "anxie@redhat.com"})

    assert resp.status_code == 502
    assert "partner0" in resp.json()["detail"]
