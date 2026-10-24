"""Exercise SDK trace ingestion without a model, subprocess, or remote server.

The SDK client is fake, but option construction, the MCP bridge, MLflow spans,
trace persistence, and metric persistence are real. Checking stored traces is
deliberate: passing an environment flag did not make this execution path emit
anything, and a mocked span assertion would not catch that regression.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import mlflow
import pytest
from claude_agent_sdk import ResultMessage, StreamEvent
from mlflow.entities import LiveSpan
from mlflow.tracking import MlflowClient

from src.agent import orchestrator, sdk_orchestrator
from src.connections import mlflow_tracking

QUESTION = "Why did fixture costs increase?"
ANSWER = "The fixture account used two instances."
CONVERSATION_ID = "fixture-conversation"
SESSION_ID = "fixture-session"
TOOL_ARGS = {"action": "get_status", "host": "fixture-host"}
TOOL_RESULT = {"instances": 2}


@pytest.fixture
def local_tracking(tmp_path, monkeypatch, request):
    """Isolate every test from developer credentials and tracking destinations."""
    from mlflow.tracking import fluent

    previous_uri = mlflow.get_tracking_uri()
    previous_experiment = fluent._active_experiment_id
    for name in (
        "MLFLOW_EXPERIMENT_ID",
        "MLFLOW_EXPERIMENT_NAME",
        "MLFLOW_TRACE_DESTINATION",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DO_NOT_TRACK", "true")
    monkeypatch.setenv("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "false")
    monkeypatch.setenv("MLFLOW_TRACE_SAMPLING_RATIO", "1.0")
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "true")
    mlflow.tracing.reset()
    uri = f"sqlite:///{tmp_path / 'tracking.db'}"
    mlflow.set_tracking_uri(uri)
    mlflow.tracing.enable()
    client = MlflowClient(tracking_uri=uri)
    experiment_name = request.node.name
    experiment_id = client.create_experiment(
        experiment_name, artifact_location=(tmp_path / "artifacts").as_uri()
    )
    config = {
        "mlflow": {"tracking_url": uri, "experiment_name": experiment_name},
        "agent": {"runtime": "sdk", "sdk": {"enabled_agents": []}},
    }
    monkeypatch.setattr("src.config.get_config", lambda: config)
    monkeypatch.setattr(mlflow_tracking, "get_config", lambda: config)
    monkeypatch.setattr(mlflow_tracking, "_client", None)
    monkeypatch.setattr(mlflow_tracking, "_experiment_name", None)
    mlflow_tracking.init_mlflow()
    yield SimpleNamespace(config=config, client=client, experiment_id=experiment_id)
    mlflow.flush_trace_async_logging()
    mlflow.tracing.reset()
    mlflow.set_tracking_uri(previous_uri)
    fluent._active_experiment_id = previous_experiment


@pytest.fixture
def fake_sdk(monkeypatch):
    """Keep real option/bridge wiring, replacing only SDK execution and tools."""
    import claude_agent_sdk

    state = SimpleNamespace(
        behavior="success", tool_error=False, tool_calls=0, tool_results=[], entered=asyncio.Event()
    )

    def decorate(name, _description, _schema):
        return lambda handler: (name, handler)

    def create_server(*, name, version, tools):
        return {"name": name, "version": version, "handlers": dict(tools)}

    async def execute_tool(name, args):
        assert name == "query_fixture"
        assert args == TOOL_ARGS
        state.tool_calls += 1
        return {"error": "fixture tool failure"} if state.tool_error else TOOL_RESULT

    class FakeClient:
        def __init__(self, options):
            self.handler = options.mcp_servers["parsec"]["handlers"]["query_fixture"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def query(self, prompt):
            assert prompt == QUESTION
            await asyncio.sleep(0)  # Let concurrent turns overlap with both roots open.

        async def receive_response(self):
            if state.behavior == "exception":
                raise RuntimeError("fixture SDK transport failed")
            if state.behavior == "cancel":
                state.entered.set()
                await asyncio.Event().wait()
            # Both calls use the real bridge; the second exercises its cache.
            state.tool_results.append(await self.handler(dict(TOOL_ARGS)))
            state.tool_results.append(await self.handler(dict(TOOL_ARGS)))
            yield StreamEvent(
                uuid="fixture-message",
                session_id="fixture-sdk-session",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": ANSWER},
                },
                parent_tool_use_id=None,
            )
            yield ResultMessage(
                subtype="error_during_execution" if state.behavior == "result_error" else "success",
                duration_ms=10,
                duration_api_ms=8,
                is_error=state.behavior == "result_error",
                num_turns=1,
                session_id="fixture-sdk-session",
                result=(
                    "fixture SDK reported failure" if state.behavior == "result_error" else ANSWER
                ),
                usage={"input_tokens": 12, "output_tokens": 8},
            )

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(claude_agent_sdk, "tool", decorate)
    monkeypatch.setattr(claude_agent_sdk, "create_sdk_mcp_server", create_server)
    monkeypatch.setattr(orchestrator, "_execute_tool", execute_tool)
    monkeypatch.setattr(sdk_orchestrator, "_orchestrator_system", lambda _cfg: "fixture system")
    monkeypatch.setattr(sdk_orchestrator, "_agent_definitions", lambda _cfg: {})
    monkeypatch.setattr(
        sdk_orchestrator,
        "_union_tool_schemas",
        lambda: [{"name": "query_fixture", "input_schema": {"type": "object"}}],
    )
    monkeypatch.setattr("src.llm.agent_sdk_client.backend_cli_env", lambda _cfg: {})
    monkeypatch.setattr("src.llm.agent_sdk_client.build_subprocess_env", lambda *_args: {})
    monkeypatch.setattr("src.llm.agent_sdk_client.default_cli_path", lambda: None)
    return state


def _turn():
    return sdk_orchestrator.run_agent_via_sdk(
        QUESTION, conversation_id=CONVERSATION_ID, session_id=SESSION_ID
    )


async def _drain_metrics():
    # Production deliberately schedules metric persistence in a background task.
    if tasks := list(orchestrator._background_tasks):
        await asyncio.gather(*tasks)


def _traces(local_tracking):
    mlflow.flush_trace_async_logging()
    return mlflow.search_traces(experiment_ids=[local_tracking.experiment_id], return_type="list")


def _one_root(local_tracking):
    traces = _traces(local_tracking)
    assert len(traces) == 1, "a configured SDK turn must persist exactly one conversation trace"
    trace = traces[0]
    roots = [span for span in trace.data.spans if span.parent_id is None]
    assert len(roots) == 1
    assert trace.info.execution_duration is not None, "the stored trace must have ended"
    return trace, roots[0]


async def test_sdk_turn_persists_trace_tool_children_and_existing_metrics(local_tracking, fake_sdk):
    events = []
    async for event in _turn():
        events.append(event)
        if event.startswith("event: done"):
            assert _one_root(local_tracking)[0].info.state.name == "OK"
    await _drain_metrics()

    trace, root = _one_root(local_tracking)
    assert trace.info.state.name == "OK"
    assert root.name == "parsec:orchestrator"
    assert root.inputs["question"] == QUESTION
    assert root.outputs["response"] == ANSWER
    assert root.attributes["runtime"] == "sdk"
    assert root.attributes["routing_method"] == "sdk"
    assert trace.info.trace_metadata["mlflow.trace.session"] == SESSION_ID
    assert trace.info.trace_metadata["conversation_id"] == CONVERSATION_ID
    children = [span for span in trace.data.spans if span.parent_id == root.span_id]
    assert len(children) == 2, "both executed and cached MCP invocations should remain visible"
    assert all(span.inputs["tool_name"] == "query_fixture" for span in children)
    assert all(span.inputs["tool_input"] == TOOL_ARGS for span in children)
    assert all(span.outputs["result"] == TOOL_RESULT for span in children)
    assert fake_sdk.tool_calls == 1, "the second MCP call must use the request cache"
    assert "event: done" in events[-1]
    assert ANSWER in "".join(events)
    runs = local_tracking.client.search_runs([local_tracking.experiment_id])
    assert len(runs) == 1
    assert runs[0].info.status == "FINISHED"
    assert runs[0].data.tags["runtime"] == "sdk"
    assert runs[0].data.metrics["input_tokens"] == 12


@pytest.mark.parametrize("behavior", ["exception", "result_error"])
async def test_sdk_failures_persist_completed_error_traces(local_tracking, fake_sdk, behavior):
    fake_sdk.behavior = behavior
    events = [event async for event in _turn()]
    await _drain_metrics()

    trace, root = _one_root(local_tracking)
    assert trace.info.state.name == "ERROR"
    assert root.status.status_code.name == "ERROR"
    assert "event: error" in "".join(events)
    assert "event: done" in events[-1]


async def test_cancelled_sdk_turn_ends_error_trace_and_propagates_cancel(local_tracking, fake_sdk):
    fake_sdk.behavior = "cancel"

    async def consume():
        return [event async for event in _turn()]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(fake_sdk.entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    trace, root = _one_root(local_tracking)
    assert trace.info.state.name == "ERROR"
    assert root.status.status_code.name == "ERROR"


async def test_closing_stream_early_ends_error_trace(local_tracking, fake_sdk):
    stream = _turn()
    await anext(stream)
    await stream.aclose()

    trace, root = _one_root(local_tracking)
    assert trace.info.state.name == "ERROR"
    assert root.status.status_code.name == "ERROR"


async def test_concurrent_turns_keep_tool_children_with_their_own_root(local_tracking, fake_sdk):
    async def consume(number):
        return [
            event
            async for event in sdk_orchestrator.run_agent_via_sdk(
                QUESTION,
                conversation_id=f"conversation-{number}",
                session_id=f"session-{number}",
            )
        ]

    await asyncio.gather(consume(1), consume(2))
    await _drain_metrics()

    traces = _traces(local_tracking)
    assert len(traces) == 2
    assert {trace.info.trace_metadata["mlflow.trace.session"] for trace in traces} == {
        "session-1",
        "session-2",
    }
    for trace in traces:
        roots = [span for span in trace.data.spans if span.parent_id is None]
        assert len(roots) == 1
        assert trace.info.state.name == "OK"
        children = [span for span in trace.data.spans if span.parent_id == roots[0].span_id]
        assert len(children) == 2
    assert fake_sdk.tool_calls == 2, "each turn must have its own cache"


async def test_tool_error_is_recorded_on_child_without_breaking_answer(local_tracking, fake_sdk):
    fake_sdk.tool_error = True
    events = [event async for event in _turn()]
    await _drain_metrics()

    trace, root = _one_root(local_tracking)
    children = [span for span in trace.data.spans if span.parent_id == root.span_id]
    assert children
    assert all(span.status.status_code.name == "ERROR" for span in children)
    assert all("fixture tool failure" in str(span.outputs) for span in children)
    assert ANSWER in "".join(events)


async def test_unconfigured_tracking_preserves_answer_without_trace(local_tracking, fake_sdk):
    local_tracking.config["mlflow"]["tracking_url"] = ""
    mlflow_tracking._client = None
    mlflow_tracking.init_mlflow()
    events = [event async for event in _turn()]
    await _drain_metrics()

    assert _traces(local_tracking) == []
    assert ANSWER in "".join(events)
    assert "event: done" in events[-1]


@pytest.mark.parametrize("failure_point", ["start", "end"])
async def test_tracing_failure_cannot_change_tool_results_or_retry_execution(
    local_tracking, fake_sdk, monkeypatch, failure_point
):
    started_spans = []
    original_start = mlflow.start_span_no_context
    original_end = LiveSpan.end

    def failing_start(*args, **kwargs):
        raise RuntimeError("fixture tracing start failure")

    def recording_start(*args, **kwargs):
        span = original_start(*args, **kwargs)
        started_spans.append(span)
        return span

    def failing_end(*args, **kwargs):
        raise RuntimeError("fixture tracing export failure")

    if failure_point == "start":
        monkeypatch.setattr(mlflow, "start_span_no_context", failing_start)
    else:
        monkeypatch.setattr(mlflow, "start_span_no_context", recording_start)
        monkeypatch.setattr(LiveSpan, "end", failing_end)

    try:
        events = [event async for event in _turn()]
        await _drain_metrics()

        assert ANSWER in "".join(events)
        assert "event: done" in events[-1]
        assert any(event.startswith("event: history") for event in events)
        assert not any(event.startswith("event: error") for event in events)
        assert fake_sdk.tool_calls == 1, "telemetry faults must not retry a completed tool"
        assert len(fake_sdk.tool_results) == 2
        for result in fake_sdk.tool_results:
            assert result["is_error"] is False
            assert json.loads(result["content"][0]["text"]) == TOOL_RESULT
        runs = local_tracking.client.search_runs([local_tracking.experiment_id])
        assert len(runs) == 1
        assert runs[0].info.status == "FINISHED"
    finally:
        # Restore/finalize the real local spans deliberately left open by the
        # injected export failure, so no global tracer state leaks to other tests.
        monkeypatch.setattr(LiveSpan, "end", original_end)
        for span in reversed(started_spans):
            original_end(span)


async def test_options_build_failure_persists_error_trace(local_tracking, fake_sdk, monkeypatch):
    def unavailable_options(*_args, **_kwargs):
        raise RuntimeError("fixture SDK options unavailable")

    monkeypatch.setattr(sdk_orchestrator, "build_orchestrator_options", unavailable_options)
    events = [event async for event in _turn()]

    trace, root = _one_root(local_tracking)
    assert trace.info.state.name == "ERROR"
    assert root.status.status_code.name == "ERROR"
    assert "fixture SDK options unavailable" in "".join(events)
    assert "event: done" in events[-1]
    assert fake_sdk.tool_calls == 0


async def test_zero_sampling_does_not_create_orphan_tool_traces(
    local_tracking, fake_sdk, monkeypatch
):
    monkeypatch.setenv("MLFLOW_TRACE_SAMPLING_RATIO", "0.0")
    mlflow.tracing.reset()
    mlflow.tracing.enable()

    events = [event async for event in _turn()]
    await _drain_metrics()

    assert _traces(local_tracking) == []
    assert fake_sdk.tool_calls == 1
    assert ANSWER in "".join(events)
    assert "event: done" in events[-1]
    assert len(local_tracking.client.search_runs([local_tracking.experiment_id])) == 1
