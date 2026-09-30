"""The SDK turn's usage record must be complete, attributable and never lost.

What the live pods logged for SDK turns (F11 in the e2e report):

* tokens from ``ResultMessage.usage`` only, which excludes every sub-agent —
  staging q13 logged in=10 out=9586, about $0.34 at list price, for a turn the
  SDK itself costed at $3.93;
* no model, because ``ResultMessage`` has no model field;
* ``agent=orchestrator`` on every line, whatever was delegated;
* no conversation id or status, so costs were paired with questions by log
  adjacency;
* nothing at all for a turn whose client went away, because the tool counts and
  ``finish()`` ran after the ``try``/``finally``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, StreamEvent, ToolUseBlock
from claude_agent_sdk._internal.message_parser import parse_message

import src.agent.parsec_mcp as bridge
import src.agent.sdk_orchestrator as orch
from src.agent.sdk_stream import SdkEventTranslator
from src.metrics.collector import MetricsCollector

#: A result message as the pinned CLI writes it on the wire, parsed by the SDK's
#: own parser so the ``modelUsage`` spelling is checked, not assumed. The field
#: names follow the CLI's result schema (inputTokens, outputTokens,
#: cacheReadInputTokens, cacheCreationInputTokens, webSearchRequests, costUSD,
#: contextWindow, maxOutputTokens per model).
CLI_RESULT = {
    "type": "result",
    "subtype": "success",
    "duration_ms": 654_000,
    "duration_api_ms": 590_000,
    "is_error": False,
    "num_turns": 9,
    "result": "Job 172261 failed in the AAP2 stage.",
    "stop_reason": "end_turn",
    "session_id": "s-q13",
    "total_cost_usd": 3.9339,
    # The orchestrator's own API calls only.
    "usage": {
        "input_tokens": 10,
        "output_tokens": 9586,
        "cache_read_input_tokens": 422_683,
        "cache_creation_input_tokens": 18_890,
    },
    # Every API call in the turn, sub-agents included, per model.
    "modelUsage": {
        "claude-sonnet-4-6": {
            "inputTokens": 1_210,
            "outputTokens": 61_586,
            "cacheReadInputTokens": 4_822_683,
            "cacheCreationInputTokens": 318_890,
            "webSearchRequests": 0,
            "costUSD": 3.70,
            "contextWindow": 200_000,
            "maxOutputTokens": 32_000,
        },
        "claude-haiku-4-5": {
            "inputTokens": 500,
            "outputTokens": 4_000,
            "cacheReadInputTokens": 100_000,
            "cacheCreationInputTokens": 20_000,
            "webSearchRequests": 0,
            "costUSD": 0.2339,
            "contextWindow": 200_000,
            "maxOutputTokens": 32_000,
        },
    },
    "permission_denials": [],
    "uuid": "0b7c5e0e-0000-4000-8000-000000000013",
}


def _flush_into(monkeypatch) -> list[MetricsCollector]:
    flushed: list[MetricsCollector] = []
    monkeypatch.setattr("src.agent.orchestrator._flush_collector", flushed.append)
    return flushed


def _finished(tr: SdkEventTranslator, monkeypatch) -> MetricsCollector:
    collector = MetricsCollector(conversation_id="conv-q13")
    _flush_into(monkeypatch)
    list(tr.finish(collector))
    return collector


# ------------------------------------------------------------------ tokens


def test_tokens_cover_every_model_the_turn_used(monkeypatch):
    tr = SdkEventTranslator(question="why did job 172261 fail?", history=[])
    list(tr.translate(parse_message(CLI_RESULT)))

    c = _finished(tr, monkeypatch)

    assert (c.input_tokens, c.output_tokens) == (1_710, 65_586)
    assert (c.cache_read_tokens, c.cache_creation_tokens) == (4_922_683, 338_890)
    assert c.cost_usd == pytest.approx(3.9339)


def test_usage_is_the_fallback_when_model_usage_is_missing(monkeypatch):
    raw = {k: v for k, v in CLI_RESULT.items() if k != "modelUsage"}
    tr = SdkEventTranslator(question="q", history=[])
    list(tr.translate(parse_message(raw)))

    c = _finished(tr, monkeypatch)

    assert (c.input_tokens, c.output_tokens) == (10, 9586)
    assert (c.cache_read_tokens, c.cache_creation_tokens) == (422_683, 18_890)


# ---------------------------------------------------------- delegations


def test_delegated_sub_agents_are_recorded_in_order_with_repeats(monkeypatch):
    tr = SdkEventTranslator(question="top GPU users?", history=[])
    for i, agent in enumerate(("cost", "cost", "babylon")):
        block = ToolUseBlock(id=f"tu-{i}", name="Agent", input={"subagent_type": agent})
        list(tr.translate(AssistantMessage(content=[block], model="claude-sonnet-4-6")))
    list(tr.translate(parse_message(CLI_RESULT)))

    c = _finished(tr, monkeypatch)

    assert c.sub_agents == "cost,cost,babylon"
    assert c.to_params()["sub_agents"] == "cost,cost,babylon"


# ------------------------------------------------------------- log line


#: run_cache_test.py's parser (parsec-parity-v2 and rhdp-parsec-integration,
#: eval/scripts), verbatim: it needs runtime, agent and the counters contiguous.
_RUN_CACHE_TEST_USAGE = re.compile(
    r"usage runtime=(?P<rt>\S+) agent=(?P<agent>\S+) in=(?P<in>\d+) out=(?P<out>\d+) "
    r"cache_read=(?P<cr>\d+) cache_write=(?P<cw>\d+) cache_hit=(?P<hit>[\d.]+)% "
    r"tools=(?P<tools>\d+) errors=(?P<errs>\d+) cost_usd=(?P<cost>[\d.]+)"
)


def _usage_line(caplog, c: MetricsCollector) -> str:
    with caplog.at_level(logging.INFO, logger="src.metrics.collector"):
        c.log_summary()
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("usage ")]
    return line


def test_usage_line_keeps_its_head_and_carries_conversation_id_and_status(caplog):
    """Existing readers match "usage runtime=" and the counters after it, so the
    new fields go on the end; the e2e harness joins on conversation_id."""
    c = MetricsCollector(conversation_id="conv-q13", runtime="sdk", agent_type="orchestrator")
    c.status = "error"
    c.record_sub_agents(["aap2", "babylon"])
    c.record_tokens(input_tokens=10, output_tokens=9586)
    c.record_cost(3.9339)

    line = _usage_line(caplog, c)

    assert line.startswith("usage runtime=sdk agent=orchestrator in=10 out=9586 ")
    m = _RUN_CACHE_TEST_USAGE.search(line)
    assert m and m["cost"] == "3.9339"
    # e2e_openshift.join_usage's own parse: whitespace-split k=v after "usage ".
    fields = dict(kv.split("=", 1) for kv in line.split("usage ", 1)[1].split() if "=" in kv)
    assert fields["conversation_id"] == "conv-q13"
    assert fields["status"] == "error"
    assert fields["sub_agents"] == "aap2,babylon"


# --------------------------------------------------- whole-turn behaviour


class _Client:
    """Stand-in for ClaudeSDKClient: one bridged tool call, one text delta, then
    either a result (a completed turn) or silence (a turn still running)."""

    finish_turn = True

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
        with patch.object(bridge, "_dispatch_cached", AsyncMock(return_value={"ok": 1})):
            await handler({"action": "get_job"})
        yield StreamEvent(
            uuid="u",
            session_id="s-q13",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
        )
        if not _Client.finish_turn:
            await asyncio.Event().wait()
        yield parse_message(CLI_RESULT)


class _Options:
    model = "claude-sonnet-4-6"


@pytest.fixture
def sdk_turn(monkeypatch):
    import claude_agent_sdk

    import src.config
    import src.metrics.collector

    made: list[MetricsCollector] = []

    def make_collector(**kw):
        made.append(MetricsCollector(**kw))
        return made[-1]

    monkeypatch.setattr(src.config, "get_config", lambda: {"agent": {"sdk": {}}})
    monkeypatch.setattr(orch, "build_orchestrator_options", lambda c, system: _Options())
    monkeypatch.setattr(orch, "_orchestrator_system", lambda c: "system")
    monkeypatch.setattr(src.metrics.collector, "MetricsCollector", make_collector)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _Client)
    flushed = _flush_into(monkeypatch)

    def run(scenario, *, finish_turn: bool) -> tuple[MetricsCollector, list[MetricsCollector]]:
        _Client.finish_turn = finish_turn
        gen = orch.run_agent_via_sdk("why did job 172261 fail?", [], conversation_id="conv-q13")
        asyncio.run(asyncio.wait_for(scenario(gen), timeout=10))
        (collector,) = made
        return collector, flushed

    return run


def test_completed_turn_is_recorded_once_with_the_configured_model(sdk_turn):
    async def scenario(gen):
        [e async for e in gen]

    c, flushed = sdk_turn(scenario, finish_turn=True)

    assert flushed == [c], "recorded exactly once, not again by the abort guard"
    assert c.status == "success"
    assert c.model == "claude-sonnet-4-6"
    assert c.to_params()["model"] == "claude-sonnet-4-6"
    assert c.tool_calls == 1
    assert c.input_tokens == 1_710


def test_closed_stream_still_records_the_turn_as_aborted(sdk_turn):
    """The response writer closes the generator at a yield (GeneratorExit)."""

    async def scenario(gen):
        async for event in gen:
            if event.startswith("event: text"):
                break
        await gen.aclose()

    c, flushed = sdk_turn(scenario, finish_turn=False)

    assert flushed == [c]
    assert c.status == "aborted"
    assert c.tool_calls == 1, "the tool counts are folded in before the record"
    assert c.conversation_id == "conv-q13"


def test_cancelled_stream_still_records_the_turn_as_aborted(sdk_turn):
    """A disconnect cancels the request task while the turn awaits the CLI."""

    async def scenario(gen):
        seen: list[str] = []

        async def consume():
            async for event in gen:
                seen.append(event)

        task = asyncio.create_task(consume())
        while not any(e.startswith("event: text") for e in seen):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # now waiting on the CLI for the next message
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    c, flushed = sdk_turn(scenario, finish_turn=False)

    assert flushed == [c]
    assert c.status == "aborted"
    assert c.tool_calls == 1
