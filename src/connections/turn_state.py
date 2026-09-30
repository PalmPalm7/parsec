"""Per-turn memory of backends that failed for a reason retrying cannot fix.

A controller that rejects Parsec's credentials (HTTP 401), or a host whose
name does not exist in DNS or that refuses the connection, will fail the same
way on every retry. Nothing remembered that within an investigation, so on the
live pods one turn hit the same dead AAP2 controller and Babylon clusters
dozens of times — 47 AAP2 and 27 Babylon 401s in a single question.

Only those three reasons are recorded (:data:`CREDENTIALS_REJECTED`,
:data:`HOST_NOT_FOUND`, :data:`CONNECTION_REFUSED`). HTTP 403 is not: on AAP2
and Kubernetes it is per-object RBAC, so one forbidden resource must not block
every other call to that controller or cluster. Nor is a connect failure that
can clear on the next call, such as a resolver timeout, a TLS error or a
connect timeout (see :func:`permanent_connect_failure`).

The agent layer opens a scope per turn with :func:`begin_turn`; connectors
record failures with :func:`mark_dead` and consult :func:`dead_reason` before
calling out. Outside a turn (tests, startup probes) there is no scope, nothing
is remembered, and every call goes through.
"""

from __future__ import annotations

import socket
from contextvars import ContextVar, Token

#: The reasons connectors record with :func:`mark_dead`. Each is a failure that
#: repeats on every call until an operator acts, and each needs a different
#: operator action, so the connectors word the follow-up error from it.
CREDENTIALS_REJECTED = "credentials rejected (HTTP 401)"
HOST_NOT_FOUND = "host name does not resolve"
CONNECTION_REFUSED = "connection refused"

_dead: ContextVar[dict[str, str] | None] = ContextVar("parsec_dead_targets", default=None)


def permanent_connect_failure(exc: BaseException) -> str | None:
    """:data:`HOST_NOT_FOUND` or :data:`CONNECTION_REFUSED` if ``exc`` is final, else ``None``.

    httpx raises the same ``ConnectError`` for every failed connection; what
    went wrong is the OS error further down the chain (httpx.ConnectError <-
    httpcore.ConnectError <- socket.gaierror, or <- OSError("All connection
    attempts failed") <- ConnectionRefusedError). Only two causes are final: a
    name that does not exist (EAI_NONAME, as for AAP2 east/west and Babylon
    babydev on staging) and a host that refuses the connection. A resolver
    hiccup (EAI_AGAIN), a TLS failure or a timeout can clear on the next call,
    and a turn can run for ten minutes, so recording those would blank out a
    working backend for the rest of the investigation.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # Match the class before the errno: ssl.SSLEOFError carries errno 8,
        # which is also EAI_NONAME on macOS.
        if isinstance(current, socket.gaierror):
            return HOST_NOT_FOUND if current.errno == socket.EAI_NONAME else None
        if isinstance(current, ConnectionRefusedError):
            return CONNECTION_REFUSED
        if isinstance(current, BaseExceptionGroup):
            # anyio tries every address a name resolves to (IPv6 and IPv4) and
            # groups the failures. Final only if every attempt failed the same
            # final way; one timed-out address could still answer next time.
            reasons = {permanent_connect_failure(e) for e in current.exceptions}
            return reasons.pop() if len(reasons) == 1 else None
        current = current.__cause__ or current.__context__
    return None


def begin_turn() -> Token[dict[str, str] | None]:
    """Start remembering dead targets for this turn. Pair with :func:`end_turn`."""
    return _dead.set({})


def end_turn(token: Token[dict[str, str] | None]) -> None:
    _dead.reset(token)


def mark_dead(target: str, reason: str) -> None:
    """Remember that ``target`` (e.g. ``"aap2:prod0"``) failed unrecoverably.

    ``reason`` is one of the constants above; connectors word the error for
    later calls from it.
    """
    dead = _dead.get()
    if dead is not None:
        dead.setdefault(target, reason)


def dead_reason(target: str) -> str | None:
    """Why ``target`` is known dead in this turn, or ``None``."""
    dead = _dead.get()
    return dead.get(target) if dead else None
