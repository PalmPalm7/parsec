"""The whole-turn SDK orchestrator path must stop at agent.sdk.turn_timeout.

``agent.sdk.timeout`` only bounded the per-agent client; run_agent_via_sdk had
no ceiling, so a CLI that stopped answering held the request open until the
browser gave up, and the user saw a spinner rather than an error.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import src.agent.sdk_orchestrator as orch


class _HangingClient:
    """Answers query() and then never produces a message."""

    instances: list[_HangingClient] = []

    def __init__(self, options):
        self.closed = False
        _HangingClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def query(self, prompt):
        return None

    async def receive_response(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover - never reached


def _events(raw: list[str]) -> list[tuple[str, dict]]:
    out = []
    for block in raw:
        name = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip() or "{}")
        if name:
            out.append((name, data or {}))
    return out


@pytest.fixture
def sdk_turn(monkeypatch):
    import claude_agent_sdk

    import src.config
    import src.metrics.collector

    def run(cfg: dict) -> list[tuple[str, dict]]:
        monkeypatch.setattr(src.config, "get_config", lambda: cfg)
        monkeypatch.setattr(orch, "build_orchestrator_options", lambda c, system: object())
        monkeypatch.setattr(orch, "_orchestrator_system", lambda c: "system")
        monkeypatch.setattr(src.metrics.collector, "MetricsCollector", MagicMock())
        monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _HangingClient)
        _HangingClient.instances.clear()

        async def collect():
            return [e async for e in orch.run_agent_via_sdk("why?", [])]

        return _events(asyncio.run(asyncio.wait_for(collect(), timeout=10)))

    return run


def test_hung_turn_is_stopped_with_one_clear_error(sdk_turn):
    events = sdk_turn({"agent": {"sdk": {"turn_timeout": 0.2}}})

    errors = [d for n, d in events if n == "error"]
    assert len(errors) == 1, errors
    assert "turn_timeout" in json.dumps(errors[0])
    names = [n for n, _ in events]
    assert names[-2:] == ["history", "done"]
    assert _HangingClient.instances[0].closed, "the CLI client must be shut down"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), (0, None), ("0", None), ("", None), (120, 120.0), ("45", 45.0), ("junk", 900.0)],
)
def test_turn_timeout_parsing(raw, expected):
    assert orch._turn_timeout({"turn_timeout": raw}) == expected


def test_turn_timeout_defaults_to_fifteen_minutes():
    assert orch._turn_timeout({}) == orch.DEFAULT_TURN_TIMEOUT_S == 900.0
