"""Both runtimes must open a dead-target scope for exactly one turn.

``src/connections/turn_state`` only remembers anything inside a scope, and
nothing opened one, so a controller that rejected Parsec's credentials was
called again and again in the same investigation — 47 AAP2 and 27 Babylon 401s
in staging q13, four partner0 401s in q07. The scope must cover the tools the
turn runs (sub-agents included) and close with the turn, so a later question
retries a backend that may have been fixed in between.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import src.agent.parsec_mcp as bridge
import src.agent.sdk_orchestrator as orch
from src.connections import turn_state

TARGET = "aap2:prod0"


class _FailingControllerClient:
    """The CLI calling a bridged tool twice; the connector marks the target dead."""

    seen: list[str | None] = []

    def __init__(self, options):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        return None

    async def receive_response(self):
        async def _dispatch(name, args):
            _FailingControllerClient.seen.append(turn_state.dead_reason(TARGET))
            turn_state.mark_dead(TARGET, "HTTP 401")
            return {"error": "HTTP 401"}

        handler = bridge._make_handler("query_aap2", allow_writes=False)
        with patch.object(bridge, "_dispatch_cached", _dispatch):
            await handler({"action": "get_job"})
            await handler({"action": "get_job"})
        return
        yield  # pragma: no cover


def test_sdk_turn_remembers_a_dead_target_until_the_turn_ends(monkeypatch):
    import claude_agent_sdk

    import src.config
    import src.metrics.collector

    monkeypatch.setattr(src.config, "get_config", lambda: {"agent": {"sdk": {}}})
    monkeypatch.setattr(orch, "build_orchestrator_options", lambda c, system: object())
    monkeypatch.setattr(orch, "_orchestrator_system", lambda c: "system")
    monkeypatch.setattr(src.metrics.collector, "MetricsCollector", MagicMock())
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FailingControllerClient)
    _FailingControllerClient.seen = []

    async def turn():
        [e async for e in orch.run_agent_via_sdk("why did job 172261 fail?", [])]
        return turn_state.dead_reason(TARGET)

    async def two_turns():
        return await turn(), await turn()

    after_first, after_second = asyncio.run(two_turns())

    # First call of each turn: nothing known. Second call: the 401 is remembered.
    assert _FailingControllerClient.seen == [None, "HTTP 401", None, "HTTP 401"]
    assert after_first is None and after_second is None, "the scope must close with the turn"


def test_legacy_turn_remembers_a_dead_target_until_the_turn_ends(monkeypatch):
    import src.agent.agents as agents
    import src.agent.orchestrator as legacy
    import src.agent.tool_definitions as tool_definitions

    seen: list[str | None] = []

    async def _sub_agent(**kwargs):
        # A tool inside the fast-path sub-agent hits the dead controller twice.
        for _ in range(2):
            seen.append(turn_state.dead_reason(TARGET))
            turn_state.mark_dead(TARGET, "HTTP 401")
        yield "event: done\ndata: {}\n\n"

    monkeypatch.setattr(legacy, "_should_orchestrate_via_sdk", lambda cfg: False)
    monkeypatch.setattr(legacy, "get_config", lambda: {})
    monkeypatch.setattr(legacy, "mlflow", MagicMock())
    monkeypatch.setattr(legacy, "set_root_span_outputs", lambda *a, **k: None)
    monkeypatch.setattr(legacy, "_flush_collector", lambda c: None)
    monkeypatch.setattr(tool_definitions, "get_orchestrator_tools", lambda: [])
    monkeypatch.setattr(agents, "classify_fast", lambda q: "aap2")
    monkeypatch.setattr(agents, "run_sub_agent_streaming", _sub_agent)

    async def turn():
        [e async for e in legacy.run_agent("why did job 172261 fail?", [])]
        return turn_state.dead_reason(TARGET)

    async def two_turns():
        return await turn(), await turn()

    after_first, after_second = asyncio.run(two_turns())

    assert seen == [None, "HTTP 401", None, "HTTP 401"]
    assert after_first is None and after_second is None, "the scope must close with the turn"
