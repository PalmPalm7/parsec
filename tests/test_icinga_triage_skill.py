"""The icinga-triage skill names only tools the SDK icinga agent is served.

On parsec-dev and the staging pod the skill's allowed-tools and workflow named
``mcp__icinga__*`` and ``mcp__github__get_file_contents``. The SDK path serves
neither: Icinga and GitHub go through Parsec's own bridge as
``mcp__parsec__query_icinga``, ``fetch_github_file`` and ``search_github_repo``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.agent import tool_definitions
from src.agent.parsec_mcp import tool_names_for
from src.skills import SkillLoader, SkillSource

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


@pytest.fixture
def served(monkeypatch) -> set[str]:
    # query_icinga is only offered when an Icinga MCP URL is configured.
    monkeypatch.setitem(tool_definitions._CONDITIONAL_TOOLS, "query_icinga", True)
    return set(tool_names_for(tool_definitions.get_icinga_tools()))


def test_allowed_tools_are_all_served_to_the_icinga_agent(served):
    loader = SkillLoader([SkillSource(label="project", root=SKILLS_ROOT)])
    skill = next(m for m in loader.load_strict() if m.name == "icinga-triage")

    assert skill.allowed_tools
    assert set(skill.allowed_tools) <= served


def test_skill_body_names_no_tool_the_agent_lacks(served):
    body = (SKILLS_ROOT / "icinga-triage" / "SKILL.md").read_text()

    named = set(re.findall(r"mcp__\w+__[\w*]+", body))

    assert named - served == set()
