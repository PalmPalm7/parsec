# MLflow tracing on the SDK runtime

With `mlflow.tracking_url` configured and startup initialization successful,
the full SDK orchestrator records one `parsec:orchestrator` trace per turn.
It includes the question, answer, runtime, model, usage, and conversation/session
identifiers. Native tools exposed by the MCP bridge are child spans, including
cache hits and tool errors. Closing or cancelling a turn ends its trace as an
error; successful traces end before the final SSE `done` event.

These application spans require no Claude CLI tracing plugin or hook. The
`MLFLOW_CLAUDE_TRACING_ENABLED` environment flag alone does not install either.
An independently configured CLI integration can provide additional internal
model/subagent details; those are outside the application span tree.

Metric runs and traces are separate MLflow records. Receiving a metric run
does not establish that the turn was traced. Missing or failed startup tracking
configuration disables both application outputs; a sampling ratio of zero
intentionally suppresses traces. Tracing failures are logged without failing
the chat response or repeating a tool execution.

## Regression verification

Run:

```sh
python -m pytest -q tests/test_sdk_tracing_integration.py
```

The tests replace the model/CLI with deterministic SDK messages, while keeping
the real option construction, MCP bridge, span creation, and MLflow persistence.
They use an isolated temporary SQLite MLflow store and query it to assert a
completed root, the correct tool-parent relationships, useful inputs/outputs,
session metadata, and the existing metric run. They need no cloud credentials,
external tracking server, or model calls and run in the normal CI pytest job.
Error, cancellation, early closure, concurrent turns, sampling, disabled tracking,
and telemetry failures are covered as well.

After deployment, also check the configured experiment for a **new trace**
after an SDK turn, and inspect its answer and tool children. Checking only the
chat answer, a metric run, or an environment variable misses this regression.
