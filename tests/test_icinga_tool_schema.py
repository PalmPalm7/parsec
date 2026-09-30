"""The query_icinga schema the model reads must describe what the tool does.

In the 2026-09-30 run the schema called ``host`` a fuzzy match and
``get_problems`` "all hosts and services in non-OK state", while the tool
matches exact Icinga names and filters get_problems by host, service and a
single name equality. The model passes what the schema tells it to.
"""

from __future__ import annotations

from src.agent.tool_definitions import TOOLS


def _schema() -> dict:
    (tool,) = [t for t in TOOLS if t["name"] == "query_icinga"]
    return tool["input_schema"]["properties"]


def test_host_is_described_as_an_exact_icinga_name():
    host = _schema()["host"]["description"]

    assert "fuzzy" not in host
    assert "Exact Icinga host name" in host
    assert "get_problems" in host
    assert "get_hosts search" in host


def test_service_is_described_as_an_exact_name_that_get_problems_honours():
    service = _schema()["service"]["description"]

    assert "Exact Icinga service name" in service
    assert "get_problems" in service


def test_get_problems_is_described_as_filtered_and_trimmed():
    action = _schema()["action"]["description"]
    get_problems = action.split("get_problems:")[1].split("get_downtimes:")[0]

    assert "filtered by host and service" in get_problems
    assert "truncated: true" in get_problems


def test_filter_expr_says_what_get_problems_does_with_it():
    filter_expr = _schema()["filter_expr"]["description"]

    assert "bracketed" in filter_expr
    assert "get_problems applies only a single host.name or service.name equality" in filter_expr
