"""SDK turn budgets: enough turns, a warning before the end, nothing thrown away.

On the live staging pod (2026-09-30 e2e) three questions ran into SDK turn caps:

* q10 — both cost sub-agents hit their 11-turn cap mid-investigation; what came
  back was narration ("Let me look up those sandbox owners…"), so the
  orchestrator re-delegated from scratch twice ($1.31 against prod's $0.30).
* q11 — the orchestrator itself stopped at ``anthropic.max_tool_rounds`` (10)
  and answered with a 156-character preamble, after its sub-agents had already
  resolved 2w27z to a ResourceClaim. The findings were discarded.
* q13 — three wasted delegations, then "start fresh with a complete
  investigation" ($3.93).

The legacy loop warns a sub-agent two rounds before its limit; the SDK path had
no equivalent.
"""

from __future__ import annotations

import json

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.agent.sdk_stream import SdkEventTranslator

_ALL = {"agent": {"runtime": "sdk", "sdk": {"enabled_agents": ["all"]}}}


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


def _options(cfg: dict):
    from src.agent.sdk_orchestrator import build_orchestrator_options

    return build_orchestrator_options(cfg, system="sys")


def _cfg(**sdk) -> dict:
    return {
        "anthropic": {"max_tool_rounds": 10},
        "agent": {"runtime": "sdk", "sdk": {"enabled_agents": ["all"], **sdk}},
    }


# ------------------------------------------------------------ turn caps


def test_orchestrator_cap_is_not_the_legacy_tool_round_budget(_sdk_stub):
    """q11 stopped at max_tool_rounds=10; the orchestrator needs its own cap."""
    assert _options(_cfg()).max_turns == 30


def test_orchestrator_cap_follows_agent_sdk_max_turns(_sdk_stub):
    assert _options(_cfg(max_turns=45)).max_turns == 45


def test_junk_orchestrator_cap_falls_back_instead_of_failing_the_turn(_sdk_stub):
    assert _options(_cfg(max_turns="lots")).max_turns == 30


def test_subagents_get_at_least_the_configured_floor(_sdk_stub):
    """cost and babylon had max_rounds 8 + 3 = 11 turns; q10's cost agents ran out."""
    from src.agent.agents import AGENTS
    from src.agent.sdk_profiles import TURN_HEADROOM

    agents = _options(_cfg()).agents

    assert agents["cost"].maxTurns == 20
    assert agents["babylon"].maxTurns == 20
    # An agent whose legacy budget is already bigger keeps it.
    assert agents["aap2"].maxTurns == AGENTS["aap2"].max_rounds + TURN_HEADROOM == 23


def test_subagent_floor_is_configurable(_sdk_stub):
    agents = _options(_cfg(subagent_min_turns=25)).agents
    assert agents["cost"].maxTurns == 25
    assert agents["aap2"].maxTurns == 25


def test_each_subagent_is_told_its_own_turn_budget(_sdk_stub):
    agents = _options(_cfg()).agents
    assert "at most 20 turns" in agents["cost"].prompt
    assert "by turn 18" in agents["cost"].prompt
    assert "at most 23 turns" in agents["aap2"].prompt


def test_orchestrator_is_told_not_to_redo_a_specialists_work():
    """q13: "Let me start fresh with a complete investigation"."""
    from src.agent.sdk_orchestrator import _orchestrator_system

    prompt = _orchestrator_system(_ALL)
    assert "do not repeat its tool calls yourself" in prompt
    assert "delegate once more with the facts it already found" in prompt


# ------------------------------------------------------- budget warning hook


def _post(agent_id: str | None, agent_type: str = "cost", event: str = "PostToolUse") -> dict:
    """A hook input shaped like the pinned CLI's (2.1.169)."""
    data = {
        "hook_event_name": event,
        "session_id": "s",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/app",
        "tool_name": "mcp__parsec__query_aws_costs",
        "tool_input": {},
        "tool_use_id": "tu",
    }
    if agent_id:
        data.update(agent_id=agent_id, agent_type=agent_type)
    return data


async def _fire(hook, data: dict, times: int) -> list[dict]:
    return [await hook(data, "tu", {"signal": None}) for _ in range(times)]


def _warned(outputs: list[dict]) -> list[int]:
    """1-based call numbers at which the hook injected the warning."""
    return [
        i + 1
        for i, out in enumerate(outputs)
        if (out.get("hookSpecificOutput") or {}).get("additionalContext")
    ]


async def test_warning_is_injected_once_three_turns_before_the_limit():
    from src.agent.agents import BUDGET_WARNING
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    outputs = await _fire(hook, _post("a1"), 20)

    assert _warned(outputs) == [17]
    out = outputs[16]["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    assert out["additionalContext"] == BUDGET_WARNING


async def test_failed_tool_calls_count_and_can_carry_the_warning():
    """A result with is_error fires PostToolUseFailure, not PostToolUse.

    Observed with the pinned CLI: a bridged tool returning is_error=True never
    reaches PostToolUse. Most staging tool calls in the long runs were 401s, so
    a hook on PostToolUse alone would have missed them.
    """
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    outputs = await _fire(hook, _post("a1", event="PostToolUseFailure"), 17)

    assert _warned(outputs) == [17]
    assert outputs[16]["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"


async def test_main_thread_calls_are_never_warned():
    """Only sub-agent hook inputs carry agent_id; the orchestrator's do not."""
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    assert _warned(await _fire(hook, _post(None), 40)) == []


async def test_parallel_agents_of_one_type_are_counted_separately():
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"babylon": 20})
    first = await _fire(hook, _post("a1", "babylon"), 10)
    second = await _fire(hook, _post("a2", "babylon"), 10)

    assert _warned(first) == [] and _warned(second) == []
    assert _warned(await _fire(hook, _post("a1", "babylon"), 7)) == [7]


async def test_unknown_agent_type_is_left_alone():
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    assert _warned(await _fire(hook, _post("a1", "general-purpose"), 40)) == []


async def test_options_wire_the_hook_to_each_subagents_real_limit(_sdk_stub):
    opts = _options(_cfg())

    assert set(opts.hooks) == {"PostToolUse", "PostToolUseFailure"}
    hook = opts.hooks["PostToolUse"][0].hooks[0]
    assert _warned(await _fire(hook, _post("c1", "cost"), 20)) == [17]
    assert _warned(await _fire(hook, _post("x1", "aap2"), 23)) == [20]


# -------------------------------------------- turn limit keeps the findings


_REPORT = (
    "## 2w27z\n\nResourceClaim `published.ai-driven-aap.prod-2w27z` → provision 44l6l "
    "→ AAP2 job 172261 failed in task `Deploy workload`."
)
_TRAILER = (
    "agentId: a2176166 (use SendMessage with to: 'a2176166' to continue this agent)\n"
    "<usage>subagent_tokens: 976\ntool_uses: 11\nduration_ms: 8406</usage>"
)


def _delegate_and_return(tr: SdkEventTranslator) -> None:
    preamble = "No results in the provisions DB for `2w27z`. Delegating to the Babylon agent."
    list(
        tr.translate(
            StreamEvent(
                uuid="u",
                session_id="s",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": preamble},
                },
            )
        )
    )
    list(
        tr.translate(
            AssistantMessage(
                content=[ToolUseBlock(id="tu-1", name="Agent", input={"subagent_type": "babylon"})],
                model="m",
            )
        )
    )
    result = ToolResultBlock(
        tool_use_id="tu-1",
        content=[{"type": "text", "text": _REPORT}, {"type": "text", "text": _TRAILER}],
    )
    list(tr.translate(UserMessage(content=[result])))


def _turn_limited(is_error: bool) -> ResultMessage:
    return ResultMessage(
        subtype="error_max_turns",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=11,
        session_id="s",
        result="",
    )


class _Collector:
    model = None

    def record_tokens(self, **kw):
        pass

    def record_cost(self, c):
        pass

    def record_model(self, m):
        pass


def _parse(raw: list[str]) -> list[tuple[str, dict]]:
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


@pytest.mark.parametrize("is_error", [False, True])
def test_turn_limit_keeps_the_specialists_reports(is_error):
    """Pinned CLI 2.1.169 reports is_error=True, result="" for error_max_turns.

    The subtype, not is_error, decides: the fallback must not hinge on a flag
    that CLI versions have set differently.
    """
    tr = SdkEventTranslator(question="what is failing with 2w27z", history=[])
    _delegate_and_return(tr)
    list(tr.translate(_turn_limited(is_error)))

    events = _parse(list(tr.finish(_Collector())))
    names = [n for n, _ in events]

    streamed = "".join(d.get("content", "") for n, d in events if n == "text")
    assert "## Partial findings (turn limit reached)" in streamed
    assert "published.ai-driven-aap.prod-2w27z" in streamed
    assert "Babylon Investigation" in streamed
    assert "SendMessage" not in streamed, "the CLI's continuation footer is not for readers"

    assert names.count("error") == 1
    assert "turn limit" in events[names.index("error")][1]["message"]
    assert names.index("text") < names.index("error")
    assert names[-2:] == ["history", "done"]

    saved = events[names.index("history")][1]["messages"][-1]["content"]
    assert saved.startswith("No results in the provisions DB")
    assert "published.ai-driven-aap.prod-2w27z" in saved


def test_a_finished_run_does_not_repeat_the_reports():
    """The orchestrator relays reports itself when it has turns left."""
    tr = SdkEventTranslator(question="q", history=[])
    _delegate_and_return(tr)
    list(
        tr.translate(
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=3,
                session_id="s",
                result="done",
            )
        )
    )
    blob = "".join(tr.finish(_Collector()))
    assert "Partial findings" not in blob
    assert "event: error" not in blob


def test_turn_limit_without_any_delegation_is_still_one_error():
    tr = SdkEventTranslator(question="q", history=[])
    list(tr.translate(_turn_limited(False)))
    events = _parse(list(tr.finish(_Collector())))
    names = [n for n, _ in events]

    assert names.count("error") == 1
    assert "Partial findings" not in json.dumps(events)
