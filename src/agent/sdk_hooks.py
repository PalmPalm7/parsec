"""Agent SDK hooks for the orchestrator session.

The legacy sub-agent loop is Parsec's own code, so it could warn a sub-agent
two rounds before its limit by appending text to the next message
(``agents._maybe_inject_budget_warning``). On the SDK the CLI runs that loop,
and a hook is the only place Parsec code sees a sub-agent's tool calls while
they happen. Without the warning, SDK sub-agents ran into their turn cap
mid-investigation: on the live staging pod, q10's two cost agents and q11's
three babylon/aap2 agents all ended on a tool call, and what reached the
orchestrator was narration such as "Let me look up those sandbox owners…".

Hook behaviour this module relies on, observed with claude-agent-sdk 0.2.106
driving the pinned CLI 2.1.169 (and the wheel's bundled 2.1.185):

* tool hooks fire for the in-process ``mcp__parsec__*`` tools, on the main
  thread and inside sub-agents;
* inside a sub-agent the hook input carries ``agent_id`` (one per spawned
  agent) and ``agent_type`` (its ``subagent_type``); on the main thread
  neither key is present;
* a call whose result has ``is_error`` set fires ``PostToolUseFailure``, not
  ``PostToolUse``, so anything that counts calls must listen to both;
* ``additionalContext`` returned from either event reaches the sub-agent's
  model with that tool result;
* a ``PreToolUse`` deny stops the call before the tool runs, and the model
  gets ``permissionDecisionReason`` back as an error tool result. Hooks run
  even for tools in ``allowed_tools``, which ``can_use_tool`` does not.

The second hook here uses that: the orchestrator's main thread may call only
its own direct tools. Every bridged tool is approved session-wide so that
sub-agents can use theirs, and on the live pods the orchestrator used that
approval to skip delegation — dev q03 ran ``query_gcp_projects`` itself, so
the cost agent and its spend workflow never loaded; dev q06 ran
``query_babylon_catalog`` inline; staging q04 ran ``query_azure_pools``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from claude_agent_sdk import HookMatcher
    from claude_agent_sdk.types import HookCallback, HookEvent

logger = logging.getLogger(__name__)

#: Warn this many turns before a sub-agent's ``maxTurns``: two more tool rounds
#: and one turn to write the report, the same margin the legacy warning leaves.
BUDGET_WARNING_MARGIN = 3

_POST_TOOL_EVENTS: tuple[HookEvent, ...] = ("PostToolUse", "PostToolUseFailure")


def budget_warning_hook(
    turn_limits: Mapping[str, int], *, margin: int = BUDGET_WARNING_MARGIN
) -> HookCallback:
    """A post-tool hook that tells a sub-agent to stop and report near its limit.

    ``turn_limits`` maps ``agent_type`` to that agent's ``maxTurns``. Tool
    calls, not turns, are counted: the hook never sees a turn boundary, and a
    turn that calls tools calls at least one, so the count reaches the
    threshold no later than the turn count would. It errs early, never late.

    State lives in the closure, so build one per orchestrator turn.
    """
    from src.agent.agents import BUDGET_WARNING

    calls: dict[str, int] = {}
    warned: set[str] = set()

    async def _hook(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        agent_id = str(input_data.get("agent_id") or "")
        if not agent_id:
            # The main thread: the orchestrator's cap is agent.sdk.max_turns,
            # and the partial-findings fallback covers it.
            return {}
        agent_type = str(input_data.get("agent_type") or "")
        limit = turn_limits.get(agent_type)
        if not limit:
            return {}
        calls[agent_id] = calls.get(agent_id, 0) + 1
        if agent_id in warned or calls[agent_id] < limit - margin:
            return {}
        warned.add(agent_id)
        logger.info(
            "SDK budget warning: %s agent %s at %d tool calls of %d turns",
            agent_type,
            agent_id,
            calls[agent_id],
            limit,
        )
        event = str(input_data.get("hook_event_name") or "PostToolUse")
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": BUDGET_WARNING}}

    return cast("HookCallback", _hook)


def delegation_guard_hook(
    direct_tools: Iterable[str], tool_owners: Mapping[str, Sequence[str]]
) -> HookCallback:
    """A pre-tool hook that refuses specialist tools on the orchestrator's thread.

    ``direct_tools`` are the ``mcp__parsec__*`` names the orchestrator may call
    itself; ``tool_owners`` maps every other bridged name to the sub-agents that
    have it, so the refusal can say where to delegate. The Reporting-MCP
    ``db_*`` tools are always the orchestrator's own, including any discovered
    after ``direct_tools`` was computed. Inside a sub-agent nothing is refused:
    ``AgentDefinition.tools`` already scopes what each one can reach.
    """
    from src.agent.parsec_mcp import SERVER_NAME

    prefix = f"mcp__{SERVER_NAME}__"
    allowed = frozenset(direct_tools)

    async def _hook(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        if input_data.get("agent_id"):
            return {}
        name = str(input_data.get("tool_name") or "")
        if not name.startswith(prefix) or name in allowed:
            return {}
        short = name[len(prefix) :]
        if short.startswith("db_"):
            return {}
        owners = list(tool_owners.get(name) or ())
        if owners:
            targets = " or ".join(f'subagent_type="{o}"' for o in owners)
            reason = (
                f"`{short}` is a specialist tool; you cannot call it yourself. Delegate "
                f"with the Agent tool ({targets}) and pass the facts you already have."
            )
        else:
            reason = (
                f"`{short}` is a specialist tool, and no specialist that uses it is "
                "enabled on this deployment. Answer with your own tools, or say that "
                "this could not be checked."
            )
        logger.info("SDK orchestrator: refused %s on the main thread", short)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return cast("HookCallback", _hook)


def build_hooks(
    *,
    turn_limits: Mapping[str, int],
    direct_tools: Iterable[str],
    tool_owners: Mapping[str, Sequence[str]],
) -> dict[HookEvent, list[HookMatcher]]:
    """The ``ClaudeAgentOptions.hooks`` mapping for one orchestrator turn."""
    from claude_agent_sdk import HookMatcher

    budget = budget_warning_hook(turn_limits)
    hooks: dict[HookEvent, list[HookMatcher]] = {
        event: [HookMatcher(hooks=[budget])] for event in _POST_TOOL_EVENTS
    }
    hooks["PreToolUse"] = [HookMatcher(hooks=[delegation_guard_hook(direct_tools, tool_owners)])]
    return hooks
