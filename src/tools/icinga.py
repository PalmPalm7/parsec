"""Tool: query_icinga — query Icinga2 monitoring via the MCP sidecar server."""

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from src.connections.icinga_mcp import call_tool

logger = logging.getLogger(__name__)

#: Most objects a get_problems result may carry; the rest are counted, not sent.
_MAX_PROBLEMS = 40

#: Most characters one check-result field (output, performance_data, command)
#: keeps in a get_problems result. A single ocpv-pvc-usage service carried
#: 149,345 characters of per-PVC performance data. The head of the output and
#: of the perfdata shows what is wrong; get_services with detailed=true
#: returns one service's whole check result.
_CHECK_FIELD_CHARS = 1_000

#: The bridge passes a tool result of up to this many characters (of its JSON)
#: to the model whole and cuts a longer one to its first 10,000. Mirrors
#: orchestrator.MAX_TOOL_RESULT_CHARS, which cannot be imported here without a
#: cycle; a test keeps the two equal.
_BRIDGE_CAP = 100_000

#: Size budget for the text of a get_problems result, measured as the bridge
#: measures it (JSON-escaped). Well under _BRIDGE_CAP, where a cut drops most
#: of the objects; the margin leaves room for the counts, hint and note.
_PROBLEMS_BUDGET = 60_000

#: Icinga attributes holding Unix timestamps. Listed by name rather than "any
#: number above 1e9" because byte counters in vars reach that size too, and
#: execution_start / schedule_* sit within a second of execution_end.
_EPOCH_FIELDS: frozenset[str] = frozenset(
    {
        "entry_time",
        "expire_time",
        "start_time",
        "end_time",
        "trigger_time",
        "remove_time",
        "last_check",
        "next_check",
        "last_state_change",
        "last_hard_state_change",
        "previous_state_change",
        "last_state_ok",
        "last_state_warning",
        "last_state_critical",
        "last_state_unknown",
        "last_state_up",
        "last_state_down",
        "last_state_unreachable",
        "acknowledgement_expiry",
        "acknowledgement_last_change",
        "flapping_last_change",
        "execution_end",
        "last_notification",
        "next_notification",
    }
)

#: The only filter_expr shape get_problems can apply itself: one name equality,
#: e.g. ``host.name == "ocpvirt7"``. The value stops at its closing quote, so a
#: compound such as ``host.name == "a" && service.name == "b"`` does not match
#: and is reported as ignored, instead of being read as one host literally
#: named ``a" && service.name == "b`` that matches nothing.
_NAME_EQUALITY = re.compile(
    r"""^\s*(host|service)\.(?:name|display_name)\s*==\s*(["'])((?:(?!\2).)*)\2\s*$"""
)


def _build_read_args(
    search: str, host: str, service: str, filter_expr: str, detailed: bool
) -> dict[str, Any]:
    """Build arguments dict for read-only query actions, omitting empty values."""
    args: dict[str, Any] = {}
    if search:
        args["search"] = search
    if host:
        args["host"] = host
    if service:
        args["service"] = service
    if filter_expr:
        # The monitoring-mcp server joins the host, service and filter clauses
        # with "&&", so an unbracketed "A || B" binds as "(host && A) || B" and
        # returns every host that matches B.
        args["filter_expr"] = f"({filter_expr})"
    args["detailed"] = detailed
    return args


_WRITE_ACTIONS_REQUIRING_OBJECT: set[str] = {
    "acknowledge_problem",
    "schedule_downtime",
    "reschedule_check",
    "add_comment",
    "remove_downtime",
    "remove_acknowledgement",
    "send_custom_notification",
}

_WRITE_ACTIONS_REQUIRING_COMMENT: set[str] = {
    "acknowledge_problem",
    "schedule_downtime",
    "add_comment",
    "send_custom_notification",
}

# Map commonly hallucinated action names to valid actions
_ACTION_ALIASES: dict[str, str] = {
    "search_alerts": "get_problems",
    "get_alerts": "get_problems",
    "get_service": "get_services",
    "get_service_details": "get_services",
    "get_service_status": "get_services",
    "get_host": "get_hosts",
    "get_host_details": "get_hosts",
    "get_host_status": "get_hosts",
    "list_hosts": "get_hosts",
    "list_services": "get_services",
    "list_problems": "get_problems",
    "list_downtimes": "get_downtimes",
    "list_comments": "get_comments",
    "check_host": "get_hosts",
    "check_service": "get_services",
}


async def query_icinga(
    action: str,
    search: str = "",
    host: str = "",
    service: str = "",
    filter_expr: str = "",
    detailed: bool = False,
    object_type: str = "",
    name: str = "",
    author: str = "parsec",
    comment: str = "",
    comment_name: str = "",
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict:
    """Dispatch an Icinga query to the appropriate MCP tool.

    Read-only actions: get_hosts, get_services, get_problems, get_downtimes,
    get_comments.

    Write actions: acknowledge_problem, schedule_downtime, reschedule_check,
    add_comment, remove_comment, remove_downtime, remove_acknowledgement,
    send_custom_notification.
    """
    original_action = action
    resolved = _ACTION_ALIASES.get(action)
    if resolved:
        logger.warning("Icinga action alias: %s -> %s", action, resolved)
        action = resolved
        if "detail" in original_action:
            detailed = True

    if action in ("get_hosts", "get_services"):
        args = _build_read_args(search, host, service, filter_expr, detailed)
        return _with_readable_result(await call_tool(action, args))

    if action == "get_problems":
        # The MCP tool takes no arguments and always returns every problem in
        # Icinga, so the caller's filters are applied to its result instead.
        raw = await call_tool("get_problems", {})
        return _filter_problems(raw, host, service, filter_expr)

    if action in ("get_downtimes", "get_comments"):
        filter_args: dict[str, Any] = {}
        if host:
            filter_args["host"] = host
        if service:
            filter_args["service"] = service
        return _with_readable_result(await call_tool(action, filter_args))

    if action == "remove_comment":
        if not comment_name:
            return {"error": "remove_comment requires comment_name"}
        return await call_tool("remove_comment", {"comment_name": comment_name})

    if action in _WRITE_ACTIONS_REQUIRING_OBJECT:
        return await _dispatch_write(
            action, object_type, name, author, comment, start_time, end_time
        )

    return {"error": f"Unknown icinga action: {action}"}


def _decoded(raw: dict[str, Any]) -> Any:
    """Return the MCP result text decoded as JSON, or None for errors and plain text."""
    text = raw.get("result")
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _encoded(data: Any) -> str:
    """Serialise like the monitoring-mcp server does, so the text reads the same."""
    return json.dumps(data, indent=2, ensure_ascii=False)


def _is_epoch(value: Any) -> bool:
    # Icinga writes 0 for "never"; the upper bound keeps millisecond values out.
    return isinstance(value, int | float) and not isinstance(value, bool) and 1e9 < value < 1e10


def _with_readable_times(value: Any, now: float, iso: bool = True) -> Any:
    """Copy ``value``, adding ``<field>_iso`` and ``<field>_age_days`` after each epoch field.

    Left with raw epoch seconds the model does the date arithmetic itself and
    gets it wrong. A negative age is in the future (next_check, a downtime's
    end_time). ``iso=False`` adds only the ages, for results short of room.
    """
    if isinstance(value, list):
        return [_with_readable_times(item, now, iso) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        out[key] = _with_readable_times(item, now, iso)
        if key in _EPOCH_FIELDS and _is_epoch(item):
            if iso:
                stamp = datetime.fromtimestamp(item, tz=UTC)
                out[f"{key}_iso"] = stamp.isoformat(timespec="seconds")
            out[f"{key}_age_days"] = round((now - item) / 86400, 1)
    return out


def _bridge_size(result: dict[str, Any]) -> int:
    """How long the bridge measures a tool result to be."""
    return len(json.dumps(result, default=str))


def _with_readable_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Add readable timestamps to a read result; errors and plain text pass through.

    The times add about 4%. They must not push a result that fitted under
    _BRIDGE_CAP over it, or the model gets the first 10,000 characters instead
    of all of it, so such a result keeps only the ages, or no times at all.
    """
    data = _decoded(raw)
    if data is None:
        return raw
    now = time.time()
    full = {**raw, "result": _encoded(_with_readable_times(data, now))}
    if _bridge_size(full) <= _BRIDGE_CAP or _bridge_size(raw) > _BRIDGE_CAP:
        return full
    ages = {**raw, "result": _encoded(_with_readable_times(data, now, iso=False))}
    return ages if _bridge_size(ages) <= _BRIDGE_CAP else raw


def _equals_any(wanted: str, names: list[Any]) -> bool:
    folded = wanted.casefold()
    return any(isinstance(n, str) and n.casefold() == folded for n in names)


def _problem_matches(obj: Any, host: str, service: str) -> bool:
    """Whether one Host or Service object from get_problems matches the filters.

    A host matches on its name or display name. A service matches on its own
    name or display name, and on its host's name (``host_name``, or the part of
    ``host!service`` before the "!"); services do not carry the host's display
    name, so that one cannot be matched here.
    """
    if not isinstance(obj, dict):
        return False
    attrs = obj.get("attrs") or {}
    host_part, bang, service_part = str(obj.get("name", "")).partition("!")
    is_service = obj.get("type") == "Service" or bool(bang)
    if host:
        names = [attrs.get("host_name"), host_part]
        if not is_service:
            names += [attrs.get("name"), attrs.get("display_name")]
        if not _equals_any(host, names):
            return False
    if service:
        if not is_service:
            return False
        if not _equals_any(service, [attrs.get("name"), attrs.get("display_name"), service_part]):
            return False
    return True


def _trimmed(value: Any) -> tuple[Any, bool]:
    """Cut a string or list longer than _CHECK_FIELD_CHARS, saying how much went."""
    if isinstance(value, str) and len(value) > _CHECK_FIELD_CHARS:
        cut = len(value) - _CHECK_FIELD_CHARS
        return f"{value[:_CHECK_FIELD_CHARS]}... [{cut} more characters trimmed]", True
    if isinstance(value, list):
        kept: list[Any] = []
        used = 0
        for item in value:
            used += len(json.dumps(item, ensure_ascii=False))
            if used > _CHECK_FIELD_CHARS:
                break
            kept.append(item)
        if len(kept) < len(value):
            return [*kept, f"... [{len(value) - len(kept)} of {len(value)} items trimmed]"], True
    return value, False


def _trimmed_problem(obj: Any) -> tuple[Any, bool]:
    """Copy one get_problems object with its long check-result fields cut short."""
    attrs = obj.get("attrs") if isinstance(obj, dict) else None
    if not isinstance(attrs, dict) or not isinstance(attrs.get("last_check_result"), dict):
        return obj, False
    check: dict[str, Any] = attrs["last_check_result"]
    short: dict[str, Any] = {}
    cut_any = False
    for key, value in check.items():
        short[key], cut = _trimmed(value)
        cut_any = cut_any or cut
    if not cut_any:
        return obj, False
    return {**obj, "attrs": {**attrs, "last_check_result": short}}, True


def _fit_problems(
    layout: dict[str, Any], entries: list[tuple[str, Any]], bare: bool, now: float
) -> tuple[str, int]:
    """Render the longest leading run of ``entries`` that fits _PROBLEMS_BUDGET.

    Returns the result text and how many entries it holds. Timestamps get both
    ``_iso`` and ``_age_days`` unless that would cost an object; then only the
    ages stay, because a whole problem is worth more than a second rendering
    of its check time.
    """

    def render(count: int, iso: bool) -> str:
        sent = entries[:count]
        shaped = {
            key: (
                [obj for section, obj in sent if section == key] if isinstance(objs, list) else objs
            )
            for key, objs in layout.items()
        }
        return _encoded(_with_readable_times(shaped[""] if bare else shaped, now, iso))

    def fits(count: int, iso: bool) -> bool:
        return len(json.dumps(render(count, iso))) <= _PROBLEMS_BUDGET

    for iso in (True, False):
        if fits(len(entries), iso):
            return render(len(entries), iso), len(entries)
    # Size grows with the count, so binary-search the largest count that fits.
    low, high = 0, len(entries)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid, False):
            low = mid
        else:
            high = mid - 1
    return render(low, False), low


def _filter_problems(
    raw: dict[str, Any], host: str, service: str, filter_expr: str
) -> dict[str, Any]:
    """Apply the caller's filters to a get_problems result and bound what is sent.

    ``filter_expr`` is honoured only as a single host or service name equality;
    anything else is reported back as ignored rather than silently dropped.
    The result is bounded by size as well as count: long check output is
    trimmed and objects past _PROBLEMS_BUDGET are counted, not sent, with
    ``truncated: true`` whenever anything was left out.
    """
    data = _decoded(raw)
    if data is None:
        return raw

    notes: list[str] = []
    if filter_expr:
        equality = _NAME_EQUALITY.match(filter_expr)
        if equality is None:
            notes.append(
                "get_problems cannot evaluate filter_expr, so it was ignored; use "
                "get_services or get_hosts for filter expressions."
            )
        elif equality.group(1) == "host":
            host = host or equality.group(3)
        else:
            service = service or equality.group(3)

    # The server returns {"hosts": [...], "services": [...]}; a bare list is
    # handled the same way as a single section.
    layout = data if isinstance(data, dict) else {"": data}
    hits = [
        (key, obj)
        for key, objs in layout.items()
        if isinstance(objs, list)
        for obj in objs
        if _problem_matches(obj, host, service)
    ]
    # Trim before adding readable times, so the budget goes on problems rather
    # than on perfdata, and so the times are never what gets cut.
    entries: list[tuple[str, Any]] = []
    cuts: list[bool] = []
    for key, obj in hits[:_MAX_PROBLEMS]:
        short, cut = _trimmed_problem(obj)
        entries.append((key, short))
        cuts.append(cut)
    text, sent = _fit_problems(layout, entries, not isinstance(data, dict), time.time())
    trimmed = any(cuts[:sent])

    out: dict[str, Any] = {**raw, "result": text}
    if trimmed or sent < len(hits):
        out["truncated"] = True
    if sent < len(hits):
        out["total_matches"] = len(hits)
        notes.append(
            f"Only {sent} of {len(hits)} matching problems are listed; pass host or "
            "service to narrow the call."
        )
    if trimmed:
        notes.append(
            f"Check-result fields longer than {_CHECK_FIELD_CHARS} characters were "
            "trimmed; get_services with host, filter_expr 'service.name == \"<name>\"' "
            "and detailed=true returns one service's full check result."
        )
    if host and not hits:
        out["hint"] = (
            f"No current problem matched these filters. If {host!r} is a dashboard display "
            f"name, look up the Icinga host name with get_hosts search={host!r} and filter "
            "on that."
        )
    if notes:
        out["note"] = " ".join(notes)
    return out


async def _dispatch_write(
    action: str,
    object_type: str,
    name: str,
    author: str,
    comment: str,
    start_time: float | None,
    end_time: float | None,
) -> dict:
    """Validate and dispatch write actions that target a host or service."""
    if not object_type or not name:
        return {"error": f"{action} requires object_type and name"}

    if action in _WRITE_ACTIONS_REQUIRING_COMMENT and not comment:
        return {"error": f"{action} requires object_type, name, and comment"}

    if action == "schedule_downtime":
        if start_time is None or end_time is None:
            return {
                "error": "schedule_downtime requires object_type, name, comment, start_time, end_time"
            }
        return await call_tool(
            action,
            {
                "object_type": object_type,
                "name": name,
                "author": author,
                "comment": comment,
                "start_time": start_time,
                "end_time": end_time,
            },
        )

    payload: dict[str, Any] = {"object_type": object_type, "name": name}
    if action in _WRITE_ACTIONS_REQUIRING_COMMENT:
        payload["author"] = author
        payload["comment"] = comment
    return await call_tool(action, payload)
