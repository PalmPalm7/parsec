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
  model with that tool result.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
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


def build_hooks(*, turn_limits: Mapping[str, int]) -> dict[HookEvent, list[HookMatcher]]:
    """The ``ClaudeAgentOptions.hooks`` mapping for one orchestrator turn."""
    from claude_agent_sdk import HookMatcher

    budget = budget_warning_hook(turn_limits)
    return {event: [HookMatcher(hooks=[budget])] for event in _POST_TOOL_EVENTS}
