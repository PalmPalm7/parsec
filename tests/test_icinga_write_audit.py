"""A write the SDK bridge permits must leave a WARNING in the log, like a refused one.

parsec-dev ran with Icinga writes enabled by a hand-set env var against the live
Icinga API ("Parsec MCP bridge: 49 tools (writes enabled)"). Refused writes were
logged; permitted ones were not, so a real acknowledge or downtime would have
left no trace in the app log. The line must name what is actually changed and
the conversation that asked for it, and model input must not be able to forge
a second line. The legacy runtime's Icinga path does not pass through the
bridge and is not covered here.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import src.agent.sdk_orchestrator as orch
from src.agent import parsec_mcp


@pytest.fixture
def ok_tool(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def _exec(name, args):
        calls.append((name, args))
        return {"ok": True}

    monkeypatch.setattr("src.agent.orchestrator._execute_tool", _exec)
    return calls


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == parsec_mcp.logger.name and r.levelno == logging.WARNING
    ]


async def test_permitted_write_is_logged_with_action_and_target(ok_tool, caplog):
    handler = parsec_mcp._make_handler("query_icinga", True)
    args = {
        "action": "acknowledge_problem",
        "object_type": "Service",
        "name": "ocpvirt7!disk",
        "comment": "known",
    }

    with caplog.at_level(logging.WARNING, logger=parsec_mcp.logger.name):
        out = await handler(args)

    assert out["is_error"] is False
    assert ok_tool, "the write itself still goes through"
    (line,) = _warnings(caplog)
    assert "acknowledge_problem" in line
    assert "Service" in line and "ocpvirt7!disk" in line


async def test_comment_removal_names_the_comment(ok_tool, caplog):
    handler = parsec_mcp._make_handler("query_icinga", True)
    with caplog.at_level(logging.WARNING, logger=parsec_mcp.logger.name):
        await handler({"action": "remove_comment", "comment_name": "ocpvirt7!parsec-1"})

    (line,) = _warnings(caplog)
    assert "remove_comment" in line and "ocpvirt7!parsec-1" in line


async def test_reads_and_other_tools_are_not_logged_as_writes(ok_tool, caplog):
    with caplog.at_level(logging.WARNING, logger=parsec_mcp.logger.name):
        await parsec_mcp._make_handler("query_icinga", True)({"action": "get_problems"})
        await parsec_mcp._make_handler("query_aap2", True)({"action": "acknowledge_problem"})

    assert _warnings(caplog) == []


async def test_a_write_is_logged_against_what_it_changes_not_a_stray_comment(ok_tool, caplog):
    """query_icinga acknowledges the Host here and ignores comment_name."""
    handler = parsec_mcp._make_handler("query_icinga", True)
    args = {
        "action": "acknowledge_problem",
        "object_type": "Host",
        "name": "ocpvirt7",
        "comment": "known",
        "comment_name": "ocpvirt7!old-1",
    }

    with caplog.at_level(logging.WARNING, logger=parsec_mcp.logger.name):
        await handler(args)

    (line,) = _warnings(caplog)
    assert "on 'Host' 'ocpvirt7'" in line
    assert "old-1" not in line


async def test_model_input_cannot_forge_a_second_audit_line(ok_tool, caplog):
    decoy = "\nWARNING Permitted Icinga write action 'reschedule_check' on Host 'decoy'"
    handler = parsec_mcp._make_handler("query_icinga", True)

    with caplog.at_level(logging.WARNING, logger=parsec_mcp.logger.name):
        await handler({"action": "reschedule_check", "object_type": f"Host{decoy}", "name": "a"})
        await handler({"action": "reschedule_check", "object_type": "Host", "name": f"a{decoy}"})

    lines = _warnings(caplog)
    assert len(lines) == 2
    assert all("\n" not in line for line in lines)


async def test_audit_line_names_the_conversation(ok_tool, caplog):
    token = parsec_mcp.turn_conversation_id.set("conv-q08")
    try:
        with caplog.at_level(logging.WARNING, logger=parsec_mcp.logger.name):
            await parsec_mcp._make_handler("query_icinga", True)(
                {"action": "remove_comment", "comment_name": "ocpvirt7!parsec-1"}
            )
    finally:
        parsec_mcp.turn_conversation_id.reset(token)

    (line,) = _warnings(caplog)
    assert "conversation_id='conv-q08'" in line


class _WritingClient:
    """Stand-in for ClaudeSDKClient whose model makes one permitted Icinga write
    through the bridge, the way the CLI calls an in-process MCP handler."""

    def __init__(self, options):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        return None

    async def receive_response(self):
        handler = parsec_mcp._make_handler("query_icinga", allow_writes=True)
        await handler({"action": "acknowledge_problem", "object_type": "Host", "name": "ocpvirt7"})
        return
        yield  # an async generator that ends without a result


def test_sdk_turn_tells_the_bridge_its_conversation(ok_tool, caplog, monkeypatch):
    import claude_agent_sdk

    import src.config

    monkeypatch.setattr(src.config, "get_config", lambda: {"agent": {"sdk": {}}})
    monkeypatch.setattr(orch, "build_orchestrator_options", lambda c, system: object())
    monkeypatch.setattr(orch, "_orchestrator_system", lambda c: "system")
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _WritingClient)
    monkeypatch.setattr("src.agent.orchestrator._flush_collector", lambda c: None)

    async def turn():
        gen = orch.run_agent_via_sdk("ack ocpvirt7", [], conversation_id="conv-q08")
        [e async for e in gen]
        return parsec_mcp.turn_conversation_id.get()

    with caplog.at_level(logging.WARNING, logger=parsec_mcp.logger.name):
        after = asyncio.run(turn())

    (line,) = _warnings(caplog)
    assert "acknowledge_problem" in line and "conversation_id='conv-q08'" in line
    assert after is None, "the conversation does not outlive its turn"
