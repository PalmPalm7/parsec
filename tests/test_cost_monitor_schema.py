"""query_cost_monitor must only advertise drill-down types the API accepts.

The schema offered drilldown_type="instance_details", and the cost-monitor API
answers it with 400 {"detail": "Invalid drilldown_type"}. On the live pods all
8 of dev q10's instance_details calls failed that way, and 2 of staging q10's.
account_services calls in the same run succeeded.
"""

from __future__ import annotations

from src.agent.tool_definitions import TOOLS


def _cost_monitor_properties() -> dict:
    (schema,) = [t for t in TOOLS if t["name"] == "query_cost_monitor"]
    return schema["input_schema"]["properties"]


def test_drilldown_type_enum_matches_the_api():
    assert _cost_monitor_properties()["drilldown_type"]["enum"] == ["account_services"]


def test_selected_key_is_an_account_not_an_instance_type():
    assert "instance type" not in _cost_monitor_properties()["selected_key"]["description"]
