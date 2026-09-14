"""How an external skill bundle is fetched.

Split out of :mod:`src.routes.skills` so the argument construction is a pure
function with no FastAPI, agent-registry or MCP imports behind it — those make
the route module expensive to import, which in practice means the fetch logic
goes untested.

The one rule worth stating: **a skill bundle may be vendored as a git
submodule.** ``rhpds/rhdp-skills-marketplace`` carries the AIOps bundle that
way, and a plain ``git clone`` of it produces an *empty* ``rhdp-rca-plugin/``
directory. Discovery then finds nothing, and the install reports success having
delivered no skills — the same silent-inert failure as a ``SKILL.md`` shipped
without its ``scripts/``.
"""

from __future__ import annotations

from pathlib import Path

#: Flags that make a clone carry submodule content. ``--shallow-submodules``
#: keeps the fetch small; it pairs with ``--depth``.
SUBMODULE_FLAGS: tuple[str, ...] = ("--recurse-submodules", "--shallow-submodules")


def clone_command(repo_url: str, ref: str, dest: Path | str) -> tuple[str, ...]:
    """The shallow, ref-pinned clone used for a normal branch or tag.

    Fails for a bare commit SHA — ``--branch`` does not accept one — which the
    caller handles by falling back to :func:`fallback_clone_command` plus a
    checkout.
    """
    return (
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        ref,
        "--single-branch",
        *SUBMODULE_FLAGS,
        repo_url,
        str(dest),
    )


def fallback_clone_command(repo_url: str, dest: Path | str) -> tuple[str, ...]:
    """Full clone, for when ``ref`` is a commit SHA and must be checked out.

    No ``--shallow-submodules`` here: the checkout moves HEAD to an arbitrary
    commit afterwards, so submodules are re-synced by
    :func:`submodule_sync_command` against whatever that commit pins.
    """
    return ("git", "clone", "--recurse-submodules", repo_url, str(dest))


def submodule_sync_command() -> tuple[str, ...]:
    """Re-point submodules at what the currently checked-out ref pins."""
    return ("git", "submodule", "update", "--init", "--recursive")
