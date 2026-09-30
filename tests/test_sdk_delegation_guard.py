"""The orchestrator's main thread may call only its own direct tools.

Every bridged tool is approved session-wide so sub-agents can use theirs, and on
the live pods (2026-09-30 e2e) the orchestrator used that approval to skip
delegation: dev q03 ran ``query_gcp_projects`` itself, so the cost agent never
loaded and spend was never checked; dev q06 ran ``query_babylon_catalog``
inline; staging q04 ran ``query_azure_pools`` before delegating anywhere.

Hook inputs are shaped as the pinned CLI (2.1.169) sends them: a sub-agent's
carry ``agent_id`` and ``agent_type``, the main thread's carry neither.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _sdk_stub(monkeypatch):
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "tool", lambda n, d, s: (lambda fn: fn), raising=False)
    monkeypatch.setattr(
        claude_agent_sdk,
        "create_sdk_mcp_server",
        lambda name, version, tools: {"name": name, "count": len(tools)},
        raising=False,
    )


def _guard(enabled: list[str] | None = None):
    from src.agent.sdk_orchestrator import build_orchestrator_options

    cfg = {"agent": {"runtime": "sdk", "sdk": {"enabled_agents": enabled or ["all"]}}}
    opts = build_orchestrator_options(cfg, system="sys")
    assert "PreToolUse" in (opts.hooks or {}), "no PreToolUse hook: specialist tools are open"
    return opts.hooks["PreToolUse"][0].hooks[0]


def _pre(tool: str, agent_type: str | None = None) -> dict:
    data = {
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/app",
        "tool_name": tool,
        "tool_input": {},
        "tool_use_id": "tu",
    }
    if agent_type:
        data.update(agent_id="a0f3", agent_type=agent_type)
    return data


async def _decide(hook, data: dict) -> tuple[str | None, str]:
    out = (await hook(data, "tu", {"signal": None})).get("hookSpecificOutput") or {}
    return out.get("permissionDecision"), out.get("permissionDecisionReason", "")


@pytest.mark.parametrize(
    "tool, delegate_to",
    [
        ("query_gcp_projects", "cost"),  # dev q03
        ("query_babylon_catalog", "babylon"),  # dev q06
        ("query_azure_pools", "cost"),  # staging q04
    ],
)
async def test_specialist_tools_are_refused_on_the_main_thread(_sdk_stub, tool, delegate_to):
    decision, reason = await _decide(_guard(), _pre(f"mcp__parsec__{tool}"))

    assert decision == "deny"
    assert "Agent" in reason
    assert f'subagent_type="{delegate_to}"' in reason


@pytest.mark.parametrize(
    "tool",
    ["query_provisions_db", "query_aws_account_db", "render_chart", "db_describe_table"],
)
async def test_the_orchestrators_own_tools_are_allowed(_sdk_stub, tool):
    assert await _decide(_guard(), _pre(f"mcp__parsec__{tool}")) == (None, "")


@pytest.mark.parametrize(
    "tool, agent_type",
    [
        ("query_gcp_projects", "cost"),
        ("query_babylon_catalog", "babylon"),
        ("query_aap2", "aap2"),
        ("query_provisions_db", "cost"),
    ],
)
async def test_nothing_is_refused_inside_a_subagent(_sdk_stub, tool, agent_type):
    assert await _decide(_guard(), _pre(f"mcp__parsec__{tool}", agent_type)) == (None, "")


@pytest.mark.parametrize("tool", ["Agent", "Skill", "ToolSearch"])
async def test_builtin_tools_are_not_the_guards_business(_sdk_stub, tool):
    assert await _decide(_guard(), _pre(tool)) == (None, "")


async def test_refusal_does_not_name_a_specialist_that_is_not_enabled(_sdk_stub):
    """With only icinga on the SDK there is nobody to delegate cost work to."""
    decision, reason = await _decide(_guard(["icinga"]), _pre("mcp__parsec__query_gcp_projects"))

    assert decision == "deny"
    assert "subagent_type" not in reason
    assert "could not be checked" in reason
