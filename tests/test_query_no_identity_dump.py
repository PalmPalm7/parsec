"""The allowed-user gate must not dump identity headers into the pod log.

Found reviewing the live deployment: every request through the allowed-user
gate logged an "SSO DEBUG" block at INFO with each X-Forwarded-*, X-Auth-*
and X-Remote-* header value — the caller's email, username and groups. The
helper was a temporary SSO debugging aid marked "TODO: Remove".
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.routes.query import _check_user_allowed

HEADERS = {
    "X-Forwarded-Email": "alice@redhat.com",
    "X-Forwarded-User": "alice",
    "X-Forwarded-Preferred-Username": "alice-sso",
    "X-Forwarded-Access-Token": "sha256~not-a-real-token",
    "X-Remote-Group": "rhpds-admins",
}


@pytest.mark.asyncio
async def test_gate_logs_no_identity_headers(monkeypatch, caplog):
    cfg = SimpleNamespace(auth={"allowed_groups": "", "allowed_users": "alice@redhat.com"})
    monkeypatch.setattr("src.routes.query.get_config", lambda: cfg)

    with caplog.at_level(logging.DEBUG, logger="src.routes.query"):
        await _check_user_allowed(SimpleNamespace(headers=HEADERS), "alice@redhat.com")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "SSO DEBUG" not in logged
    for name, value in HEADERS.items():
        assert name not in logged
        assert value not in logged
