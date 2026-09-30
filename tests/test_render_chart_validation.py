"""render_chart must refuse a chart with nothing to show.

Charts are rendered client-side from the tool input. An empty or all-zero
dataset used to be passed straight through and sent as a chart event, which
the UI draws as an empty frame — indistinguishable from a broken chart, and
usually the sign of an empty query result the answer should state in words.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.orchestrator import _execute_tool, _yield_output_events


def _chart(*datasets: list) -> dict:
    return {
        "chart_type": "bar",
        "title": "GPU spend by user",
        "labels": ["a", "b", "c"],
        "datasets": [{"label": f"s{i}", "data": d} for i, d in enumerate(datasets)],
    }


@pytest.fixture(autouse=True)
def _no_db_tools(monkeypatch):
    monkeypatch.setattr("src.agent.orchestrator._execute_db_tool", AsyncMock(return_value=None))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_input", "reason"),
    [
        ({"chart_type": "bar", "title": "t", "labels": [], "datasets": []}, "no data points"),
        ({"chart_type": "bar", "title": "t", "labels": ["a"]}, "no data points"),
        (_chart([], []), "no data points"),
        (_chart([0, 0, 0]), "every data point is 0"),
        (_chart([0, 0.0, 0], [0, 0, 0]), "every data point is 0"),
    ],
    ids=["no-datasets", "datasets-missing", "empty-data", "all-zero", "all-zero-multi"],
)
async def test_empty_or_all_zero_chart_is_an_error(tool_input, reason):
    result = await _execute_tool("render_chart", tool_input)

    assert reason in result["error"]
    assert _yield_output_events("render_chart", result) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_input",
    [_chart([12.5, 0, 3]), _chart([0, 0, 0], [0, 4, 0]), _chart([-5, 0, 0])],
    ids=["some-zero", "one-nonzero-dataset", "negative"],
)
async def test_chart_with_data_is_returned_as_is(tool_input):
    result = await _execute_tool("render_chart", tool_input)

    assert result is tool_input
    assert len(_yield_output_events("render_chart", result)) == 1
