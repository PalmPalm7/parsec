"""Separate text blocks must not run together in the answer.

On the live pods the orchestrator's narration before a delegation and its answer
after it were joined with nothing in between — "…in parallel.## GCP Open
Environment" (staging q03), ".Now" (dev q06), ".Three" (dev q13) — so a
markdown heading rendered as plain text glued to the previous sentence, and the
saved conversation kept the glue.
"""

from __future__ import annotations

import json

import pytest
from claude_agent_sdk import StreamEvent

from src.agent.sdk_stream import SdkEventTranslator
from src.metrics.collector import MetricsCollector


def _stream(event: dict, parent: str | None = None) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s-1", event=event, parent_tool_use_id=parent)


def _text_block(index: int, text: str, parent: str | None = None) -> list[StreamEvent]:
    """One text content block as the Messages API streams it."""
    return [
        _stream(
            {"type": "content_block_start", "index": index, "content_block": {"type": "text"}},
            parent,
        ),
        _stream(
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text},
            },
            parent,
        ),
        _stream({"type": "content_block_stop", "index": index}, parent),
    ]


def _tool_block(index: int) -> list[StreamEvent]:
    return [
        _stream(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "tool_use", "id": "tu-1", "name": "Agent"},
            }
        ),
        _stream({"type": "content_block_stop", "index": index}),
    ]


def _streamed_text(tr: SdkEventTranslator, messages: list[StreamEvent]) -> str:
    out = []
    for message in messages:
        for event in tr.translate(message):
            if event.startswith("event: text"):
                out.append(json.loads(event.split("data: ", 1)[1])["content"])
    return "".join(out)


@pytest.fixture
def tr() -> SdkEventTranslator:
    return SdkEventTranslator(question="GCP and AWS spend for sandbox5560?", history=[])


def test_narration_and_answer_are_separate_paragraphs(tr, monkeypatch):
    narration = "I'll check GCP and AWS in parallel."
    answer = "## GCP Open Environment\nNo spend."
    # Message 1: narration, then the delegation. Message 2: the answer.
    streamed = _streamed_text(tr, [*_text_block(0, narration), *_tool_block(1)])
    streamed += _streamed_text(tr, _text_block(0, answer))

    expected = f"{narration}\n\n{answer}"
    assert streamed == expected, "the browser renders what is streamed"
    assert tr.answer == expected

    monkeypatch.setattr("src.agent.orchestrator._flush_collector", lambda c: None)
    history = next(
        e for e in tr.finish(MetricsCollector(conversation_id="c")) if "event: history" in e
    )
    saved = json.loads(history.split("data: ", 1)[1])["messages"][-1]["content"]
    if isinstance(saved, list):
        saved = "".join(b.get("text", "") for b in saved if isinstance(b, dict))
    assert saved == expected, "the saved conversation must not keep the glue either"


def test_first_block_gets_no_leading_blank_line(tr):
    assert _streamed_text(tr, _text_block(0, "Answer.")) == "Answer."


def test_existing_line_breaks_are_topped_up_not_doubled(tr):
    _streamed_text(tr, _text_block(0, "Checking.\n"))
    _streamed_text(tr, _text_block(0, "Done."))
    assert tr.answer == "Checking.\n\nDone."

    tr2 = SdkEventTranslator(question="q", history=[])
    _streamed_text(tr2, _text_block(0, "Checking.\n\n"))
    _streamed_text(tr2, _text_block(0, "Done."))
    assert tr2.answer == "Checking.\n\nDone."


def test_subagent_blocks_do_not_break_the_answer(tr):
    """Sub-agent prose is not streamed, so its blocks must not add breaks either."""
    _streamed_text(tr, _text_block(0, "Working on it"))
    _streamed_text(tr, _text_block(0, "internal notes", parent="tu-1"))
    assert tr.answer == "Working on it"


def test_non_text_block_start_adds_nothing(tr):
    _streamed_text(tr, _text_block(0, "Narration."))
    assert _streamed_text(tr, _tool_block(1)) == ""
    assert tr.answer == "Narration."
