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

import asyncio
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


class _SubagentRun:
    """A sub-agent run whose transcript is laid out and written as CLI 2.1.169 does.

    The CLI passes the orchestrator's ``transcript_path`` and ``session_id`` in
    every hook input; the sub-agent's own transcript sits beside it under
    ``<session_id>/subagents/agent-<agent_id>.jsonl``. Each tool_use block of a
    response is written as its own ``assistant`` line carrying the response's
    ``message.id``, then its tool_result. The CLI buffers those writes: in two
    local runs a fast tool's own line was on disk for 1 of 27 PostToolUse
    events, and landed about 100 ms later.
    """

    def __init__(self, root, agent_id: str = "a1", agent_type: str = "cost") -> None:
        self.agent_id, self.agent_type = agent_id, agent_type
        self.main = root / "s.jsonl"
        self.path = root / "s" / "subagents" / f"agent-{agent_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.turn = 0
        self._write({"type": "user", "message": {"role": "user", "content": "task"}})

    def _write(self, entry: dict) -> None:
        entry.update(isSidechain=True, agentId=self.agent_id, sessionId="s")
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _assistant(self, msg_id: str, block: dict) -> None:
        self._write(
            {
                "type": "assistant",
                "message": {"id": msg_id, "role": "assistant", "content": [block]},
            }
        )

    def hook_input(self, tool_use_id: str, event: str = "PostToolUse") -> dict:
        return {
            "hook_event_name": event,
            "session_id": "s",
            "transcript_path": str(self.main),
            "cwd": "/app",
            "tool_name": "mcp__parsec__query_aws_costs",
            "tool_input": {},
            "tool_use_id": tool_use_id,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
        }

    async def turn_with(
        self, hook, calls: int, event: str = "PostToolUse", lag: float | None = 0.0
    ) -> list[dict]:
        """One model response making ``calls`` parallel tool calls.

        ``lag=0`` writes each call's line before its hook fires. A positive
        ``lag`` writes it that many seconds after the hook fires, as the CLI's
        buffered writer does; ``None`` only after the hook has returned.
        """
        loop = asyncio.get_running_loop()
        self.turn += 1
        msg_id = f"msg_{self.turn:03d}"
        self._assistant(msg_id, {"type": "text", "text": "Checking."})
        outputs = []
        for i in range(calls):
            tool_use_id = f"tu_{self.turn}_{i}"
            use = {"type": "tool_use", "id": tool_use_id, "name": "q", "input": {}}
            pending = [True]

            def write_use(msg_id=msg_id, use=use, pending=pending) -> None:
                if pending[0]:
                    pending[0] = False
                    self._assistant(msg_id, use)

            if lag == 0:
                write_use()
            elif lag is not None:
                loop.call_later(lag, write_use)
            outputs.append(await hook(self.hook_input(tool_use_id, event), tool_use_id, {}))
            write_use()  # the line precedes the tool_result, whenever it lands
            result = {"type": "tool_result", "tool_use_id": tool_use_id, "content": "x" * 50}
            self._write({"type": "user", "message": {"role": "user", "content": [result]}})
        return outputs

    async def turns(self, hook, n: int, calls: int = 3, **kw) -> list[list[dict]]:
        return [await self.turn_with(hook, calls, **kw) for _ in range(n)]


def _warned_turns(per_turn: list[list[dict]]) -> list[tuple[int, int]]:
    """(turn, call within turn), 1-based, wherever the warning was injected."""
    return [
        (t + 1, c + 1)
        for t, outputs in enumerate(per_turn)
        for c, out in enumerate(outputs)
        if (out.get("hookSpecificOutput") or {}).get("additionalContext")
    ]


def _post(agent_id: str | None, agent_type: str = "cost", event: str = "PostToolUse") -> dict:
    """A hook input whose transcript does not exist, so no turn can be read."""
    data = {
        "hook_event_name": event,
        "session_id": "s",
        "transcript_path": "/nonexistent/parsec-test/t.jsonl",
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


async def test_warning_counts_turns_when_each_turn_makes_parallel_calls(tmp_path):
    """Live sub-agents make 2-3 calls per turn; the warning must still wait for turn 17.

    Counting calls instead warned the staging cost agents at turn 6-7 of 20
    (call 17), and the model stops as soon as it is told to.
    """
    from src.agent.agents import BUDGET_WARNING
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    per_turn = await _SubagentRun(tmp_path).turns(hook, 20, calls=3)

    assert _warned_turns(per_turn) == [(17, 1)]
    out = per_turn[16][0]["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    assert out["additionalContext"] == BUDGET_WARNING


async def test_one_call_per_turn_is_warned_at_the_same_turn(tmp_path):
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    assert _warned_turns(await _SubagentRun(tmp_path).turns(hook, 20, calls=1)) == [(17, 1)]


async def test_a_call_whose_line_lands_while_the_hook_waits_gets_its_own_turn(tmp_path):
    """The CLI writes a call's line after its PostToolUse, but the turn's text before.

    Counting the unwritten call as one more turn then counted the turn twice and
    warned at turn 16 of a local run's widget agent; near the threshold the
    hook waits for the line instead.
    """
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    per_turn = await _SubagentRun(tmp_path).turns(hook, 20, calls=3, lag=0.03)

    assert _warned_turns(per_turn) == [(17, 1)]


async def test_a_line_that_never_lands_is_counted_early_not_late(tmp_path, monkeypatch):
    """If the wait runs out, count the next turn: warning a turn early still
    leaves a turn for the report, a turn late may not."""
    import src.agent.sdk_hooks as sdk_hooks

    monkeypatch.setattr(sdk_hooks, "_FLUSH_WAIT_S", 0.03)
    hook = sdk_hooks.budget_warning_hook({"cost": 20})
    per_turn = await _SubagentRun(tmp_path).turns(hook, 20, calls=3, lag=None)

    assert _warned_turns(per_turn) == [(16, 1)]


async def test_calls_far_from_the_threshold_do_not_wait_for_the_transcript(tmp_path, monkeypatch):
    import src.agent.sdk_hooks as sdk_hooks

    monkeypatch.setattr(sdk_hooks, "_FLUSH_WAIT_S", 5.0)
    hook = sdk_hooks.budget_warning_hook({"cost": 20})
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _SubagentRun(tmp_path).turns(hook, 14, calls=3, lag=None)

    assert loop.time() - started < 1.0


async def test_failed_tool_calls_count_and_can_carry_the_warning(tmp_path):
    """A result with is_error fires PostToolUseFailure, not PostToolUse.

    Observed with the pinned CLI: a bridged tool returning is_error=True never
    reaches PostToolUse. Most staging tool calls in the long runs were 401s, so
    a hook on PostToolUse alone would have missed them.
    """
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    per_turn = await _SubagentRun(tmp_path).turns(hook, 17, calls=2, event="PostToolUseFailure")

    assert _warned_turns(per_turn) == [(17, 1)]
    assert per_turn[16][0]["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"


async def test_unreadable_transcript_falls_back_to_twice_the_calls():
    """No transcript: assume two calls per turn, so warn at call 34, not call 17."""
    from src.agent.sdk_hooks import FALLBACK_CALLS_PER_TURN, budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    assert _warned(await _fire(hook, _post("a1"), 40)) == [17 * FALLBACK_CALLS_PER_TURN] == [34]


async def test_input_without_a_transcript_path_falls_back_too():
    from src.agent.sdk_hooks import budget_warning_hook

    data = _post("a1")
    del data["transcript_path"]
    hook = budget_warning_hook({"cost": 20})
    assert _warned(await _fire(hook, data, 40)) == [34]


def test_turn_counter_leaves_a_half_written_line_for_the_next_read(tmp_path):
    from src.agent.sdk_hooks import _TurnCounter

    path = tmp_path / "agent-a1.jsonl"
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "tool_use", "id": "t1"}]},
        }
    )
    path.write_text(line[:25])
    counter = _TurnCounter(path)

    assert counter.read() and counter.turns == 0
    path.write_text(line + "\n")
    assert counter.read() and counter.turns == 1
    assert counter.turn_of("t1") == 1


def test_turn_counter_ignores_lines_it_cannot_parse(tmp_path):
    from src.agent.sdk_hooks import _TurnCounter

    path = tmp_path / "agent-a1.jsonl"
    good = {"type": "assistant", "message": {"id": "m1", "content": []}}
    path.write_text('{"type": "assistant", broken\n["assistant"]\n' + json.dumps(good) + "\n")

    counter = _TurnCounter(path)
    assert counter.read() and counter.turns == 1


async def test_main_thread_calls_are_never_warned():
    """Only sub-agent hook inputs carry agent_id; the orchestrator's do not."""
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    assert _warned(await _fire(hook, _post(None), 80)) == []


async def test_parallel_agents_of_one_type_are_counted_separately(tmp_path):
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"babylon": 20})
    first = _SubagentRun(tmp_path, "a1", "babylon")
    second = _SubagentRun(tmp_path, "a2", "babylon")
    early = await first.turns(hook, 10) + await second.turns(hook, 10)

    assert _warned_turns(early) == []
    assert _warned_turns(await first.turns(hook, 7)) == [(7, 1)]


async def test_unknown_agent_type_is_left_alone(tmp_path):
    from src.agent.sdk_hooks import budget_warning_hook

    hook = budget_warning_hook({"cost": 20})
    run = _SubagentRun(tmp_path, agent_type="general-purpose")
    assert _warned_turns(await run.turns(hook, 20)) == []


async def test_options_wire_the_hook_to_each_subagents_real_limit(_sdk_stub, tmp_path):
    opts = _options(_cfg())

    assert {"PostToolUse", "PostToolUseFailure"} <= set(opts.hooks)
    hook = opts.hooks["PostToolUse"][0].hooks[0]
    cost = _SubagentRun(tmp_path, "c1", "cost")
    aap2 = _SubagentRun(tmp_path, "x1", "aap2")
    assert _warned_turns(await cost.turns(hook, 20)) == [(17, 1)]
    assert _warned_turns(await aap2.turns(hook, 23)) == [(20, 1)]


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
