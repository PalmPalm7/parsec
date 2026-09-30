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
  even for tools in ``allowed_tools``, which ``can_use_tool`` does not;
* a sub-agent's own transcript is
  ``<dirname(transcript_path)>/<session_id>/subagents/agent-<agent_id>.jsonl``
  (``transcript_path`` is the orchestrator's). Each tool_use block of one model
  response is its own ``"type": "assistant"`` line, and all of them carry that
  response's ``message.id``, so distinct ids count turns. The CLI buffers those
  writes: a line reached disk about 100 ms after it was made, so when a fast
  tool's ``PostToolUse`` fires its own line is usually not on disk yet (26 of 27
  calls in two local runs), and the turn's text line only sometimes is. The
  CLI keeps flushing while it waits for a hook's answer.

The second hook here uses that: the orchestrator's main thread may call only
its own direct tools and the generic GitHub reads. Every bridged tool is approved session-wide so that
sub-agents can use theirs, and on the live pods the orchestrator used that
approval to skip delegation — dev q03 ran ``query_gcp_projects`` itself, so
the cost agent and its spend workflow never loaded; dev q06 ran
``query_babylon_catalog`` inline; staging q04 ran ``query_azure_pools``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from claude_agent_sdk import HookMatcher
    from claude_agent_sdk.types import HookCallback, HookEvent

logger = logging.getLogger(__name__)

#: Warn this many turns before a sub-agent's ``maxTurns``: two more tool rounds
#: and one turn to write the report, the same margin the legacy warning leaves.
BUDGET_WARNING_MARGIN = 3

#: Tool calls per turn assumed when a sub-agent's transcript cannot be read.
#: Live sub-agents batch their calls: the staging cost and babylon agents made
#: 1-4 per turn, mostly 2-3. Counting one call as one turn warned them at turn
#: 6-9 of 20, and they obeyed at once, so a call count alone gives back the
#: early cut-off the 20-turn floor removed.
FALLBACK_CALLS_PER_TURN = 2

#: How long a hook waits for the CLI to write the call's own transcript line,
#: and how often it looks. Only a call within one turn of the threshold waits:
#: until its line lands, the call may belong to the last turn on disk or to the
#: next, and guessing either way warns a turn early or a turn late.
_FLUSH_WAIT_S = 0.5
_FLUSH_POLL_S = 0.02

_POST_TOOL_EVENTS: tuple[HookEvent, ...] = ("PostToolUse", "PostToolUseFailure")

#: Specialist tools the orchestrator may still call on its own thread: generic,
#: read-only GitHub reads that belong to no one domain. q12 ("summarize the
#: rhpds/parsec README") was answered well on dev and staging with one
#: fetch_github_file call from the main thread. Refusing it costs an
#: orchestrator turn for the refusal, then a specialist with its domain prompt
#: and preloaded skills, just to read a README.
MAIN_THREAD_READ_TOOLS = frozenset({"fetch_github_file", "search_github_repo"})


def _subagent_transcript(input_data: Any) -> Path | None:
    """Where the CLI writes this sub-agent's own transcript, if the input says."""
    transcript = str(input_data.get("transcript_path") or "")
    session_id = str(input_data.get("session_id") or "")
    agent_id = str(input_data.get("agent_id") or "")
    if not (transcript and session_id and agent_id):
        return None
    return Path(transcript).parent / session_id / "subagents" / f"agent-{agent_id}.jsonl"


class _TurnCounter:
    """One sub-agent's turns, read from its transcript a few new lines at a time.

    A turn is one model response. The CLI writes each of its tool_use blocks as
    a separate ``assistant`` line, all with the response's ``message.id``, so
    three parallel calls are one id and one turn.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        #: message id -> 1-based turn number, in the order the turns appear.
        self._turns: dict[str, int] = {}
        #: tool_use id -> the turn that made it.
        self._calls: dict[str, int] = {}

    @property
    def turns(self) -> int:
        """Turns on disk so far."""
        return len(self._turns)

    def turn_of(self, tool_use_id: str) -> int | None:
        return self._calls.get(tool_use_id)

    def read(self) -> bool:
        """Take in whatever the CLI has written since; False if it cannot be read."""
        try:
            with self._path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return False
        # Whole lines only. A line the CLI is still writing is read next time.
        end = chunk.rfind(b"\n") + 1
        for line in chunk[:end].splitlines():
            # Tool results are the long lines and never "assistant" entries;
            # skip parsing most of them.
            if b'"assistant"' in line:
                self._take(line)
        self._offset += end
        return True

    async def wait_for(self, tool_use_id: str, timeout: float) -> int | None:
        """The turn that made ``tool_use_id``, once its line lands, or None."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (turn := self.turn_of(tool_use_id)) is None and loop.time() < deadline:
            await asyncio.sleep(_FLUSH_POLL_S)
            if not self.read():
                return None
        return turn

    def _take(self, line: bytes) -> None:
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            return
        message = entry.get("message")
        if not isinstance(message, dict) or not message.get("id"):
            return
        turn = self._turns.setdefault(str(message["id"]), len(self._turns) + 1)
        for block in message.get("content") or ():
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                self._calls[str(block["id"])] = turn


def budget_warning_hook(
    turn_limits: Mapping[str, int], *, margin: int = BUDGET_WARNING_MARGIN
) -> HookCallback:
    """A post-tool hook that tells a sub-agent to stop and report near its limit.

    ``turn_limits`` maps ``agent_type`` to that agent's ``maxTurns``. The
    warning goes out once per agent, with the first tool result of turn
    ``maxTurns - margin``. Turns come from the sub-agent's transcript, because
    the hook itself sees calls, not turns. If the transcript cannot be read,
    it fires at ``FALLBACK_CALLS_PER_TURN`` times that many calls instead.

    State lives in the closure, so build one per orchestrator turn.
    """
    from src.agent.agents import BUDGET_WARNING

    calls: dict[str, int] = {}
    counters: dict[str, _TurnCounter] = {}
    warned: set[str] = set()

    async def _turn(agent_id: str, input_data: Any, tool_use_id: str, threshold: int) -> int | None:
        """The turn this call belongs to, or at least whether it reaches ``threshold``.

        ``None`` when the transcript cannot be read or holds no turn yet.
        """
        counter = counters.get(agent_id)
        if counter is None:
            path = _subagent_transcript(input_data)
            if path is None:
                return None
            counter = counters[agent_id] = _TurnCounter(path)
        if not counter.read() or not counter.turns:
            return None
        turn = counter.turn_of(tool_use_id)
        if turn is not None:
            return turn
        # Not on disk yet, so this call is in the last turn on disk or the next.
        latest = counter.turns + 1
        if latest < threshold:
            return latest
        # Either answer decides the warning, so wait for the line. If it never
        # lands, count the next turn: one turn early is safe, one late is not.
        return await counter.wait_for(tool_use_id, _FLUSH_WAIT_S) or latest

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
        if agent_id in warned:
            return {}
        threshold = limit - margin
        call_id = str(tool_use_id or input_data.get("tool_use_id") or "")
        try:
            turn = await _turn(agent_id, input_data, call_id, threshold)
        except Exception:  # a hook that raises would fail the tool call it follows
            logger.warning("SDK budget warning: cannot count turns of %s", agent_id, exc_info=True)
            turn = None
        if turn is not None:
            if turn < threshold:
                return {}
            basis = f"turn {turn}"
        else:
            if calls[agent_id] < threshold * FALLBACK_CALLS_PER_TURN:
                return {}
            basis = f"call {calls[agent_id]} (transcript unreadable)"
        warned.add(agent_id)
        logger.info(
            "SDK budget warning: %s agent %s at %s of %d turns",
            agent_type,
            agent_id,
            basis,
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
    after ``direct_tools`` was computed, and so are ``MAIN_THREAD_READ_TOOLS``.
    Inside a sub-agent nothing is refused:
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
        if short.startswith("db_") or short in MAIN_THREAD_READ_TOOLS:
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
