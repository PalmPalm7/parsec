"""Per-turn memory of backends that failed for a reason retrying cannot fix.

A controller that rejects Parsec's credentials (HTTP 401/403) or a hostname
that no longer resolves will fail the same way on every retry. Nothing
remembered that within an investigation, so on the live pods one turn hit the
same dead AAP2 controller and Babylon clusters dozens of times — 47 AAP2 and 27
Babylon 401s in a single question.

The agent layer opens a scope per turn with :func:`begin_turn`; connectors
record failures with :func:`mark_dead` and consult :func:`dead_reason` before
calling out. Outside a turn (tests, startup probes) there is no scope, nothing
is remembered, and every call goes through.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_dead: ContextVar[dict[str, str] | None] = ContextVar("parsec_dead_targets", default=None)


def begin_turn() -> Token[dict[str, str] | None]:
    """Start remembering dead targets for this turn. Pair with :func:`end_turn`."""
    return _dead.set({})


def end_turn(token: Token[dict[str, str] | None]) -> None:
    _dead.reset(token)


def mark_dead(target: str, reason: str) -> None:
    """Remember that ``target`` (e.g. ``"aap2:prod0"``) failed unrecoverably."""
    dead = _dead.get()
    if dead is not None:
        dead.setdefault(target, reason)


def dead_reason(target: str) -> str | None:
    """Why ``target`` is known dead in this turn, or ``None``."""
    dead = _dead.get()
    return dead.get(target) if dead else None
