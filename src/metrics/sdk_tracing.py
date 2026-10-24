"""Best-effort, explicitly parented spans for the streaming SDK runtime.

Do not hold an active tracing context across async-generator yields: the SDK
dispatches tools in other tasks, and a stream may be closed by another task.
Bound parent spans give each request its own tree without CLI hooks or plugins.
"""

from __future__ import annotations

import logging
from typing import Any

import mlflow

from src.connections.mlflow_tracking import get_mlflow_client

logger = logging.getLogger(__name__)


def start_sdk_span(
    name: str,
    span_type: str,
    inputs: dict,
    *,
    parent_span: Any = None,
    attributes: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
) -> Any:
    """Start only when application tracking initialized; never fall back to disk."""
    if get_mlflow_client() is None:
        return None
    try:
        return mlflow.start_span_no_context(
            name=name,
            span_type=span_type,
            parent_span=parent_span,
            inputs=inputs,
            attributes=attributes,
            metadata=metadata,
        )
    except Exception:
        logger.warning("Could not start SDK trace span %s (non-fatal)", name, exc_info=True)
        return None


def end_sdk_span(
    span: Any,
    outputs: dict,
    *,
    attributes: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    """Always finish spans, including failed/cancelled operations, without raising."""
    if span is None:
        return
    try:
        span.end(
            outputs=outputs,
            attributes={**(attributes or {}), **({"error": error} if error else {})},
            status="ERROR" if error else "OK",
        )
    except Exception:
        logger.warning("Could not end SDK trace span (non-fatal)", exc_info=True)
