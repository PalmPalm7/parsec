"""SDK orchestrator turns must report real tool counts, latency and status.

Every SDK turn on the live pods logged ``tools=0 errors=0 latency_ms=0`` — a
204-tool-call investigation included — because the SDK runs the tool loop
itself, nothing fed the collector, and the timer was never started. MLflow
therefore could not tell a cheap turn from a runaway one, or a failed turn from
a good one.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

import src.agent.parsec_mcp as bridge
import src.agent.sdk_orchestrator as orch
from src.metrics.collector import MetricsCollector


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("result", "calls", "errors"),
    [({"rows": [1]}, 1, 0), ({"error": "HTTP 401"}, 1, 1)],
)
def test_bridge_counts_calls_and_errors(result, calls, errors):
    handler = bridge._make_handler("query_aap2", allow_writes=False)
    stats = bridge.ToolStats()
    token = bridge.tool_stats.set(stats)
    try:
        with patch.object(bridge, "_dispatch_cached", AsyncMock(return_value=result)):
            _run(handler({"action": "get_job"}))
    finally:
        bridge.tool_stats.reset(token)
    assert (stats.calls, stats.errors) == (calls, errors)


def test_refused_write_counts_as_a_failed_call():
    handler = bridge._make_handler("query_icinga", allow_writes=False)
    stats = bridge.ToolStats()
    token = bridge.tool_stats.set(stats)
    try:
        _run(handler({"action": "acknowledge_problem"}))
    finally:
        bridge.tool_stats.reset(token)
    assert (stats.calls, stats.errors) == (1, 1)


def test_bridge_without_stats_still_works():
    handler = bridge._make_handler("query_aap2", allow_writes=False)
    with patch.object(bridge, "_dispatch_cached", AsyncMock(return_value={"ok": 1})):
        out = _run(handler({}))
    assert out["is_error"] is False


class _ToolCallingClient:
    """Makes three bridged tool calls (one failing) from inside the stream, like the CLI."""

    def __init__(self, options):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        return None

    async def receive_response(self):
        handler = bridge._make_handler("query_aap2", allow_writes=False)
        results = [{"ok": 1}, {"error": "HTTP 401"}, {"ok": 2}]
        with patch.object(bridge, "_dispatch_cached", AsyncMock(side_effect=results)):
            for _ in results:
                await handler({"action": "get_job"})
        time.sleep(0.02)
        return
        yield  # pragma: no cover


def test_sdk_turn_feeds_tool_counts_latency_and_status(monkeypatch):
    import claude_agent_sdk

    import src.config
    import src.metrics.collector

    captured: list[MetricsCollector] = []

    def make_collector(**kw):
        c = MetricsCollector(**kw)
        captured.append(c)
        return c

    monkeypatch.setattr(src.config, "get_config", lambda: {"agent": {"sdk": {}}})
    monkeypatch.setattr(orch, "build_orchestrator_options", lambda c, system: object())
    monkeypatch.setattr(orch, "_orchestrator_system", lambda c: "system")
    monkeypatch.setattr(src.metrics.collector, "MetricsCollector", make_collector)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _ToolCallingClient)
    monkeypatch.setattr("src.agent.orchestrator._flush_collector", lambda c: c.stop_timer())

    async def collect():
        return [e async for e in orch.run_agent_via_sdk("why?", [])]

    _run(collect())

    (c,) = captured
    assert (c.tool_calls, c.tool_errors) == (3, 1)
    assert c.total_latency_ms > 0
    # No ResultMessage arrived, so the turn failed — and says so.
    assert c.status == "error"
