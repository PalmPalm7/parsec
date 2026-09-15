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


#: A marketplace may expose an aggregate directory of every skill alongside the
#: per-bundle directories. In the RHDP Skills Marketplace that aggregate is
#: ``skills/`` and each entry is a directory holding a single *symlink* to the
#: canonical ``SKILL.md`` — no scripts, no references. Copying from it therefore
#: reproduces exactly the failure this installer exists to prevent: a lone
#: SKILL.md whose ``scripts/`` never arrives. Canonical roots are searched
#: first and the aggregate is only a fallback for skills found nowhere else.
AGGREGATE_ROOT_NAME = "skills"

#: Directories never searched for skills: VCS metadata, build output, and the
#: RCA bundle's ``experiments/``, which holds prompt variants that were never
#: meant to ship.
_SKIP_DIRS = frozenset({".git", ".github", ".claude-plugin", "experiments", "node_modules"})


def discover_skill_roots(clone_dir: Path | str) -> list[Path]:
    """Every directory-of-skill-directories in a cloned bundle.

    Returns canonical per-bundle roots (``<bundle>/skills/``) first, then the
    top-level aggregate (``skills/``) if present, so a caller that de-duplicates
    by skill name keeps the copy that still has its scripts.

    Only two levels are searched. Going deeper would sweep in vendored trees and
    test fixtures, and every real bundle layout seen so far is one of these two.
    """
    base = Path(clone_dir)
    canonical: list[Path] = []
    aggregate: list[Path] = []

    top = base / AGGREGATE_ROOT_NAME
    if _holds_skills(top):
        aggregate.append(top)

    try:
        children = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return aggregate

    for child in children:
        if child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        nested = child / AGGREGATE_ROOT_NAME
        if _holds_skills(nested):
            canonical.append(nested)

    return canonical + aggregate


def _holds_skills(root: Path) -> bool:
    """True iff ``root`` directly contains at least one ``<dir>/SKILL.md``.

    Name alone is not enough to identify a skill root: ``docs/skills/`` in the
    RHDP marketplace holds Jekyll pages *about* skills, and a repo is free to
    put anything under a directory called ``skills``.
    """
    if not root.is_dir():
        return False
    try:
        return any((child / "SKILL.md").is_file() for child in root.iterdir() if child.is_dir())
    except OSError:
        return False
