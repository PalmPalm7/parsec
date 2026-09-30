"""A pod that is not ready must say so in the status code, not only the body.

Found on a live deployment: /api/health/ready answered 200 with
``{"status": "not_ready"}`` when Reporting MCP discovery had not succeeded.
The kubelet reads only the status code, so the readiness probe always passed
and a pod without its DB tools stayed in the Service.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import src.routes.health as health


@pytest.fixture()
def not_ready(monkeypatch):
    """Reporting MCP configured, discovery not (yet) succeeded; no real retry."""
    monkeypatch.setattr(health.reporting_mcp, "get_mcp_url", lambda: "http://mcp:8080/mcp")
    monkeypatch.setattr(health.reporting_mcp, "get_server_instructions", lambda: "")
    monkeypatch.setattr(health.reporting_mcp, "get_mcp_tools", lambda: [])
    fetch = AsyncMock(return_value="")
    monkeypatch.setattr(health.reporting_mcp, "fetch_server_instructions", fetch)
    monkeypatch.setattr(health, "_mcp_retry", None, raising=False)
    monkeypatch.setattr(health, "_mcp_retry_at", 0.0, raising=False)
    return fetch


@pytest.fixture()
def client(monkeypatch):
    from src.app import app

    @asynccontextmanager
    async def _noop_lifespan(app_):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan)
    return TestClient(app, raise_server_exceptions=False)


def test_not_ready_is_a_503(client, not_ready):
    resp = client.get("/api/health/ready")

    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "db": "reporting_mcp_not_initialized"}


def test_liveness_stays_200_while_not_ready(client, not_ready):
    assert client.get("/api/health").status_code == 200


@pytest.mark.asyncio
async def test_not_ready_retries_discovery_in_the_background(not_ready):
    """Startup discovery runs once; without a retry a 503 pod would never rejoin."""
    resp = await health.readiness()
    await asyncio.sleep(0)

    assert resp.status_code == 503
    not_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_is_rate_limited(not_ready, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])

    await health.readiness()
    await asyncio.sleep(0)
    clock[0] += 10  # next probe, inside the retry interval
    await health.readiness()
    await asyncio.sleep(0)
    assert not_ready.await_count == 1

    clock[0] += health._MCP_RETRY_INTERVAL_S
    await health.readiness()
    await asyncio.sleep(0)
    assert not_ready.await_count == 2


@pytest.mark.asyncio
async def test_no_second_retry_while_one_is_in_flight(not_ready, monkeypatch):
    gate = asyncio.Event()

    async def _slow() -> str:
        await gate.wait()
        return ""

    slow = AsyncMock(side_effect=_slow)
    monkeypatch.setattr(health.reporting_mcp, "fetch_server_instructions", slow)
    monkeypatch.setattr(health, "_MCP_RETRY_INTERVAL_S", 0.0)

    await health.readiness()
    await asyncio.sleep(0)
    await health.readiness()
    await asyncio.sleep(0)
    assert slow.await_count == 1

    gate.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_ready_does_not_touch_the_mcp(not_ready, monkeypatch):
    monkeypatch.setattr(health.reporting_mcp, "get_mcp_tools", lambda: [{"name": "db_query"}])

    resp = await health.readiness()

    assert resp == {"status": "ready", "db": "via_reporting_mcp"}
    not_ready.assert_not_awaited()
