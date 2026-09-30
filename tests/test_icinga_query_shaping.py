"""Regression tests for how query_icinga shapes monitoring-mcp results.

Payloads mirror what the live monitoring-mcp sidecar returned in the 2026-09-30
OpenShift end-to-end run: ``get_problems`` answers ``{"hosts": [...],
"services": [...]}`` for all of Icinga whatever the caller asked, and every
timestamp is a raw epoch float.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.agent.orchestrator import MAX_TOOL_RESULT_CHARS
from src.tools.icinga import (
    _BRIDGE_CAP,
    _MAX_PROBLEMS,
    _PROBLEMS_BUDGET,
    _with_readable_times,
    query_icinga,
)

# A comment the live deployment returned for replica4; its age at the run was
# 57 days, which the model reported as "~62 days" and "~9 months".
_ENTRY_TIME = 1785871211.720291
_RUN_TIME = _ENTRY_TIME + 57 * 86400


def _service(
    host: str, name: str, state: int = 2, output: str = "CRITICAL", perfdata: list | None = None
) -> dict:
    return {
        "attrs": {
            "host_name": host,
            "name": name,
            "state": state,
            "last_check_result": {
                "execution_start": 1790794049.671225,
                "execution_end": 1790794049.747915,
                "output": output,
                "performance_data": perfdata or [],
            },
        },
        "joins": {},
        "meta": {},
        "name": f"{host}!{name}",
        "type": "Service",
    }


def _host(name: str, display_name: str) -> dict:
    return {
        "attrs": {"name": name, "display_name": display_name, "state": 1},
        "joins": {},
        "meta": {},
        "name": name,
        "type": "Host",
    }


def _problems_payload() -> dict:
    return {
        "result": json.dumps(
            {
                "hosts": [_host("ocpvirt7", "ocpv07 IBM Cloud"), _host("infra02", "infra02")],
                "services": [
                    _service("ocpvirt6", "ocpv-pvc-usage"),
                    _service("ocpvirt7", "ocp-virt-status"),
                    _service("ocpvirt8", "ocp-virt-status"),
                    _service("ocpvirt7", "odf_osd_util", state=1),
                    _service("qa.infra.opentlc.com", "gpte-api-health"),
                ],
            },
            indent=2,
        )
    }


async def _problems(**kwargs) -> tuple[dict, dict]:
    with patch("src.tools.icinga.call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _problems_payload()
        out = await query_icinga("get_problems", **kwargs)
    mock_call.assert_called_once_with("get_problems", {})
    return out, json.loads(out["result"])


def _names(body: dict) -> list[str]:
    return [o["name"] for o in body["hosts"] + body["services"]]


# ---------------------------------------------------------------------------
# filter_expr precedence (dev q08: host=ocpv07 + "A || B" returned 8 hosts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_filter_expr_is_bracketed_before_the_server_ands_it(mock_call):
    mock_call.return_value = {"result": "[]"}
    expr = 'match("*OSD*", service.display_name) || match("*ODF*", service.display_name)'

    await query_icinga("get_services", host="ocpvirt7", filter_expr=expr, detailed=True)

    args = mock_call.call_args[0][1]
    assert args["filter_expr"] == f"({expr})"
    assert args["host"] == "ocpvirt7"


# ---------------------------------------------------------------------------
# get_problems filtering (dev q08 / staging q08: 388,847 chars for 14 hosts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_problems_keeps_only_the_requested_host():
    out, body = await _problems(host="ocpvirt7")

    assert _names(body) == ["ocpvirt7", "ocpvirt7!ocp-virt-status", "ocpvirt7!odf_osd_util"]
    assert "truncated" not in out


@pytest.mark.asyncio
async def test_get_problems_host_matches_a_host_objects_display_name():
    _, body = await _problems(host="OCPV07 IBM Cloud")

    assert _names(body) == ["ocpvirt7"]


@pytest.mark.asyncio
async def test_get_problems_applies_a_host_equality_filter_expr():
    # The exact call the model made in dev q08 L75.
    out, body = await _problems(filter_expr='host.name == "ocpvirt7"')

    assert _names(body) == ["ocpvirt7", "ocpvirt7!ocp-virt-status", "ocpvirt7!odf_osd_util"]
    assert "note" not in out


@pytest.mark.asyncio
async def test_get_problems_filters_by_service_and_drops_host_objects():
    _, body = await _problems(service="ocp-virt-status")

    assert _names(body) == ["ocpvirt7!ocp-virt-status", "ocpvirt8!ocp-virt-status"]


@pytest.mark.asyncio
async def test_get_problems_says_when_it_ignored_a_filter_expr():
    out, body = await _problems(filter_expr="service.state == 2")

    assert len(_names(body)) == 7
    assert "ignored" in out["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expr",
    [
        'host.name == "ocpvirt7" && service.name == "odf_osd_util"',
        'host.name == "ocpvirt7" || host.name == "ocpvirt8"',
        "host.name == 'ocpvirt7' || host.name == 'ocpvirt8'",
    ],
)
async def test_get_problems_ignores_a_compound_filter_expr_instead_of_misreading_it(expr):
    # The model writes compound filter_exprs freely (5 of the 29 captured q05/q08
    # query_icinga calls). Read as one host equality they matched nothing, which
    # is a false "no problem" for a host that has one.
    out, body = await _problems(filter_expr=expr)

    assert len(_names(body)) == 7
    assert "ignored" in out["note"]
    assert "hint" not in out


@pytest.mark.asyncio
async def test_get_problems_miss_on_a_display_name_points_at_get_hosts():
    # "ocpv07" is only part of the dashboard display name; the host is ocpvirt7.
    out, body = await _problems(host="ocpv07")

    assert _names(body) == []
    assert "get_hosts search='ocpv07'" in out["hint"]


@pytest.mark.asyncio
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_get_problems_is_capped_and_says_so(mock_call):
    services = [_service(f"host{i}", "check") for i in range(_MAX_PROBLEMS + 10)]
    mock_call.return_value = {"result": json.dumps({"hosts": [], "services": services})}

    out = await query_icinga("get_problems")

    body = json.loads(out["result"])
    assert len(body["services"]) == _MAX_PROBLEMS
    assert out["truncated"] is True
    assert out["total_matches"] == _MAX_PROBLEMS + 10


def _pvc_usage(host: str) -> dict:
    """A service shaped like ocpvirt8!ocpv-pvc-usage: 2,017 perfdata items, 149k chars."""
    perfdata = [f"sandbox-{i:05d}-ocp4-cluster/prime-{i:032x}=41%;80;90;0;100" for i in range(2017)]
    output = "[CRITICAL] 1 unhealthy PVC/PV: sandbox-ltrsl-ocp4-cluster (Lost)\n" + "x" * 5000
    return _service(host, "ocpv-pvc-usage", output=output, perfdata=perfdata)


@pytest.mark.asyncio
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_get_problems_trims_long_check_fields_and_says_so(mock_call):
    # host=ocpvirt8 on the live payload was 3 objects and 192,825 chars; the
    # bridge cut it to its first 10,000, so later services never arrived.
    big = _pvc_usage("ocpvirt8")
    mock_call.return_value = {
        "result": json.dumps({"hosts": [], "services": [big, _service("ocpvirt8", "later")]})
    }

    out = await query_icinga("get_problems", host="ocpvirt8")

    services = json.loads(out["result"])["services"]
    assert [s["name"] for s in services] == ["ocpvirt8!ocpv-pvc-usage", "ocpvirt8!later"]
    check = services[0]["attrs"]["last_check_result"]
    assert check["output"].startswith("[CRITICAL] 1 unhealthy PVC/PV")
    assert check["output"].endswith("more characters trimmed]")
    perfdata = check["performance_data"]
    assert perfdata[0] == big["attrs"]["last_check_result"]["performance_data"][0]
    assert perfdata[-1] == f"... [{2017 - (len(perfdata) - 1)} of 2017 items trimmed]"
    # Times are added after trimming, so they are never what gets cut.
    assert "execution_end_iso" in check
    assert out["truncated"] is True
    assert "detailed=true" in out["note"]
    assert len(json.dumps(out)) < 20_000


@pytest.mark.asyncio
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_get_problems_is_bounded_by_size_not_only_count(mock_call):
    services = [_pvc_usage(f"ocpvirt{i}") for i in range(_MAX_PROBLEMS)]
    mock_call.return_value = {"result": json.dumps({"hosts": [], "services": services})}

    out = await query_icinga("get_problems")

    assert len(json.dumps(out["result"])) <= _PROBLEMS_BUDGET
    assert len(json.dumps(out)) < MAX_TOOL_RESULT_CHARS
    sent = [s["name"] for s in json.loads(out["result"])["services"]]
    assert 0 < len(sent) < _MAX_PROBLEMS
    assert sent == [s["name"] for s in services[: len(sent)]]
    assert out["truncated"] is True
    assert out["total_matches"] == _MAX_PROBLEMS
    assert f"Only {len(sent)} of {_MAX_PROBLEMS}" in out["note"]


@pytest.mark.asyncio
@patch("src.tools.icinga.time.time", return_value=_RUN_TIME)
async def test_get_problems_drops_iso_times_before_it_drops_a_problem(_clock):
    full, full_body = await _problems()
    assert "execution_end_iso" in full_body["services"][0]["attrs"]["last_check_result"]

    with patch("src.tools.icinga._PROBLEMS_BUDGET", len(json.dumps(full["result"])) - 1):
        out, body = await _problems()

    assert _names(body) == _names(full_body)
    check = body["services"][0]["attrs"]["last_check_result"]
    assert "execution_end_iso" not in check
    assert "execution_end_age_days" in check
    assert "truncated" not in out


@pytest.mark.asyncio
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_get_problems_passes_errors_through(mock_call):
    mock_call.return_value = {"error": "Icinga MCP call failed: boom"}

    out = await query_icinga("get_problems", host="ocpvirt7")

    assert out == {"error": "Icinga MCP call failed: boom"}


# ---------------------------------------------------------------------------
# Readable timestamps (q05: a 57-day-old comment read as "~62 days"/"~9 months")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.tools.icinga.time.time", return_value=_RUN_TIME)
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_comment_entry_time_gets_iso_and_age(mock_call, _clock):
    comment = {
        "attrs": {
            "author": "claude-code",
            "entry_time": _ENTRY_TIME,
            "host_name": "replica4.ops.demo.redhat.com",
            "service_name": "check_ipa_healthcheck",
        },
        "name": "replica4.ops.demo.redhat.com!check_ipa_healthcheck!a960de12",
        "type": "Comment",
    }
    mock_call.return_value = {"result": json.dumps([comment], indent=2)}

    out = await query_icinga("get_comments", host="replica4.ops.demo.redhat.com")

    attrs = json.loads(out["result"])[0]["attrs"]
    assert attrs["entry_time"] == _ENTRY_TIME
    assert attrs["entry_time_iso"] == "2026-08-04T19:20:11+00:00"
    assert attrs["entry_time_age_days"] == 57.0


@pytest.mark.asyncio
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_service_check_times_get_iso_nested(mock_call):
    mock_call.return_value = {"result": json.dumps([_service("ocpvirt7", "odf_osd_util")])}

    out = await query_icinga("get_services", host="ocpvirt7", detailed=True)

    check = json.loads(out["result"])[0]["attrs"]["last_check_result"]
    assert check["execution_end_iso"] == "2026-09-30T18:47:29+00:00"
    assert "execution_end_age_days" in check
    # Within a second of execution_end: not worth the extra keys.
    assert "execution_start_iso" not in check


def test_bridge_cap_mirrors_the_orchestrator():
    assert _BRIDGE_CAP == MAX_TOOL_RESULT_CHARS


@pytest.mark.asyncio
@patch("src.tools.icinga.time.time", return_value=_RUN_TIME)
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_readable_times_never_push_a_result_over_the_bridge_cap(mock_call, _clock):
    # Dev q08 had get_services results of 98,893 and 98,855 characters, under
    # the cap. The times took them to about 103k and the model got 11k of each.
    raw = {"result": json.dumps([_service("ocpvirt7", "odf_osd_util")], indent=2)}
    mock_call.return_value = raw

    async def read() -> dict:
        return await query_icinga("get_services", host="ocpvirt7", detailed=True)

    full = await read()

    # Room for the ages but not the ISO strings: the ages stay.
    with patch("src.tools.icinga._BRIDGE_CAP", len(json.dumps(full)) - 1):
        ages = await read()
    check = json.loads(ages["result"])[0]["attrs"]["last_check_result"]
    assert "execution_end_iso" not in check
    assert "execution_end_age_days" in check

    # Room for the result as it came, and nothing more: it goes as it came.
    with patch("src.tools.icinga._BRIDGE_CAP", len(json.dumps(raw))):
        assert await read() == raw

    # Over the cap either way: the bridge cuts it regardless, so keep the times.
    with patch("src.tools.icinga._BRIDGE_CAP", len(json.dumps(raw)) - 1):
        assert await read() == full


def test_readable_times_sit_next_to_their_field_and_skip_non_timestamps():
    now = 1790793917.5
    value = {
        "last_check": now - 86400,
        "last_state_change": 0,  # Icinga's "never"
        "next_check": now + 2 * 86400,
        "vars": {"disk_bytes": 2_000_000_000, "entry_time": "not a number"},
        "acknowledgement": True,
    }

    out = _with_readable_times(value, now=now)

    assert list(out)[:3] == ["last_check", "last_check_iso", "last_check_age_days"]
    assert out["last_check_age_days"] == 1.0
    assert out["next_check_age_days"] == -2.0  # in the future
    assert "last_state_change_iso" not in out
    assert out["vars"] == {"disk_bytes": 2_000_000_000, "entry_time": "not a number"}


@pytest.mark.asyncio
@patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
async def test_plain_text_results_pass_through(mock_call):
    mock_call.return_value = {"result": "No hosts found"}

    out = await query_icinga("get_hosts", search="nothing")

    assert out == {"result": "No hosts found"}
