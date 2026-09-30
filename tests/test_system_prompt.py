"""Tests for src.agent.system_prompt — which prompt files each agent is built from.

The live e2e run on 2026-09-30 found the orchestrator writing SQL without the
column pitfalls: they lived in shared_context.md, which only sub-agents load, and
the orchestrator guessed ``provisions.user_email`` in 7 questions. These tests pin
the pitfalls to every agent and keep the wrong tower_job_log fact from coming back.
"""

from __future__ import annotations

import os

import pytest

from src.agent import system_prompt
from src.agent.system_prompt import _AGENT_PROMPT_FILES, get_agent_prompt, get_prompt_files

USER_EMAIL_RULE = "`provisions` has NO `email` or `user_email` column"


@pytest.fixture(autouse=True)
def _isolated_loader(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # No Reporting MCP and no local learnings file: the prompt is built from the
    # repo's prompt files alone, and nothing is served from an earlier test's cache.
    monkeypatch.setattr("src.connections.reporting_mcp.get_server_instructions", lambda: "")
    monkeypatch.setattr(system_prompt, "_LEARNINGS_PATH", str(tmp_path / "no-learnings.md"))
    monkeypatch.setattr(system_prompt, "_agent_prompt_cache", {})


@pytest.mark.parametrize("agent_type", sorted(_AGENT_PROMPT_FILES))
def test_every_agent_gets_the_db_pitfalls_once(agent_type: str) -> None:
    prompt = get_agent_prompt(agent_type)
    assert prompt.count(USER_EMAIL_RULE) == 1


def test_orchestrator_prompt_has_the_user_email_rule_and_claim_fallback() -> None:
    prompt = get_agent_prompt("orchestrator")
    assert USER_EMAIL_RULE in prompt
    # staging q11: 2w27z was only findable as a ResourceClaim name suffix.
    assert "resource_claim_name LIKE '%-<guid>'" in prompt


@pytest.mark.parametrize("agent_type", sorted(_AGENT_PROMPT_FILES))
def test_no_prompt_claims_tower_job_log_is_snake_case(agent_type: str) -> None:
    # db_describe_table on the live DB: "deployerJob", "towerHost", "towerJobURL",
    # and no provision_uuid. An unquoted deployerJob fails as deployerjob.
    prompt = get_agent_prompt(agent_type)
    assert "deployer_job" not in prompt
    assert '`"deployerJob"`' in prompt


def test_prompt_files_list_the_pitfalls_for_the_orchestrator() -> None:
    assert "db_pitfalls.md" in get_prompt_files("orchestrator")
    assert "shared_context.md" not in get_prompt_files("orchestrator")


def test_editing_the_pitfalls_invalidates_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    pitfalls = tmp_path / "db_pitfalls.md"
    pitfalls.write_text("## Pitfalls v1\n")
    monkeypatch.setattr(system_prompt, "_DB_PITFALLS_PATH", str(pitfalls))

    assert "Pitfalls v1" in get_agent_prompt("orchestrator")

    pitfalls.write_text("## Pitfalls v2\n")
    stat = pitfalls.stat()
    os.utime(pitfalls, (stat.st_atime, stat.st_mtime + 10))

    prompt = get_agent_prompt("orchestrator")
    assert "Pitfalls v2" in prompt
    assert "Pitfalls v1" not in prompt
