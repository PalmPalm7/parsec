"""A permitted Icinga write must leave a WARNING in the log, like a refused one.

parsec-dev ran with Icinga writes enabled by a hand-set env var against the live
Icinga API ("Parsec MCP bridge: 49 tools (writes enabled)"). Refused writes were
logged; permitted ones were not, so a real acknowledge or downtime would have
left no trace in the app log.
"""

from __future__ import annotations

import logging

import pytest

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
