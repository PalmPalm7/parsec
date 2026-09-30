"""Tests for src.agent.system_prompt — which prompt files each agent is built from.

The live e2e run on 2026-09-30 found the orchestrator writing SQL without the
column pitfalls: they lived in shared_context.md, which only sub-agents load, and
the orchestrator guessed ``provisions.user_email`` in 7 questions. These tests pin
the pitfalls to every agent and keep the wrong tower_job_log fact from coming back.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.agent import system_prompt
from src.agent.system_prompt import _AGENT_PROMPT_FILES, get_agent_prompt, get_prompt_files

USER_EMAIL_RULE = "`provisions` has NO `email` or `user_email` column"
# describe_table provisions: ordered_by is a nullable FK to users.email, and it differs
# from the user_id user's email in about 37% of last month's rows.
ORDERED_BY_FACT = (
    "p.ordered_by is the requester's email (FK to users.email; may be NULL). "
    "p.user_id → users is the assigned user and can differ."
)
PROVISION_LOOKUP_SKILL = Path(__file__).resolve().parent.parent / "skills/provision-lookup/SKILL.md"


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


def _section(prompt: str, heading: str, next_heading: str) -> str:
    start = prompt.index(heading)
    return prompt[start : prompt.index(next_heading, start)]


def _flat(text: str) -> str:
    """Collapse line wrapping so assertions do not depend on where Markdown wraps."""
    return " ".join(text.split())


def test_aap2_flow_picks_the_job_for_the_failed_action() -> None:
    # provisions.tower_job_id is the provision job in 1274 of 1274 rows and a
    # stop/start/destroy job in 0 of 1633: sent there for a destroy failure, the agent
    # read the provision job — which had usually succeeded — and was told to stop.
    flow = _section(get_agent_prompt("aap2"), "### Investigation Flow", "### Available")

    assert "No Babylon call is needed" not in flow
    assert "WHERE provision_uuid = '<uuid>' AND action = '<action>'" in flow
    assert 'ORDER BY "startTimestamp" DESC LIMIT 1' in flow
    # The per-action DB lookup comes before any Babylon call.
    assert flow.index("FROM provision_job") < flow.index("list_anarchy_subjects")
    assert "`tower_jobs.<action>`" in flow


def test_orchestrator_keeps_the_general_no_rows_delegate_rule() -> None:
    # The orchestrator does not load shared_context.md, so this is its only copy of
    # "zero rows → hand off to Babylon". Narrowed to GUIDs, a name or other identifier
    # that found nothing had no stop rule and the model kept guessing columns.
    prompt = get_agent_prompt("orchestrator")
    rule = prompt[prompt.index("If any provisions DB lookup returns no rows") :]
    rule = _flat(rule[: rule.index("\n- ")])

    assert "delegate to `investigate_babylon` next" in rule
    assert "do NOT retry the provisions DB" in rule
    # The ResourceClaim check is the one exception, and it applies to GUIDs only.
    assert "for a GUID that matched no `babylon_guid`, first check `resource_claim_log" in rule
    # The sandbox-warning bullet must not send a GUID to Babylon before that check.
    warnings = _flat(_section(prompt, "**High instance count", "**Multi-domain"))
    assert "delegate to `investigate_babylon` first" not in warnings
    assert "after the one `resource_claim_log` check" in warnings


@pytest.mark.parametrize("agent_type", sorted(_AGENT_PROMPT_FILES))
def test_every_prompt_says_ordered_by_is_the_requester_email(agent_type: str) -> None:
    # The old text called ordered_by the requester's "name", so the model joined
    # users on user_id for "who ordered it" — a different person in ~37% of rows.
    prompt = get_agent_prompt(agent_type)
    assert prompt.count(ORDERED_BY_FACT) == 1
    assert "user's name is in `ordered_by`" not in prompt


def test_provision_lookup_skill_says_ordered_by_is_the_requester_email() -> None:
    skill = PROVISION_LOOKUP_SKILL.read_text()
    assert ORDERED_BY_FACT in skill
    assert "user's name is in `ordered_by`" not in skill


# db_describe_table in staging q13: every mixed-case column of tower_job_log and
# provision_job. Each one fails unquoted, exactly as "deployerJob" did.
QUOTED_JOB_COLUMNS = (
    "deployerJob",
    "towerHost",
    "towerJobURL",
    "jobStatus",
    "startTimestamp",
    "completeTimestamp",
)


@pytest.mark.parametrize("column", QUOTED_JOB_COLUMNS)
def test_pitfalls_list_every_camel_case_job_column(column: str) -> None:
    # The orchestrator has no other source for these, so check its prompt: a model
    # asking "when did the job run" reaches for startTimestamp before anything else.
    pitfalls = get_agent_prompt("orchestrator")
    pitfalls = _flat(pitfalls[pitfalls.index("## Provision Database: Column Pitfalls") :])
    assert f'`"{column}"`' in pitfalls


def test_pitfalls_say_the_other_job_columns_are_snake_case() -> None:
    # "column names are camelCase" invited quoting provision_uuid and action too.
    pitfalls = get_agent_prompt("orchestrator")
    pitfalls = _flat(pitfalls[pitfalls.index("## Provision Database: Column Pitfalls") :])
    assert "column names are camelCase" not in pitfalls
    assert "Every other column in both tables is lowercase snake_case" in pitfalls


@pytest.mark.parametrize("agent_type", sorted(set(_AGENT_PROMPT_FILES) - {"orchestrator"}))
def test_rate_limit_fallback_uses_a_table_that_joins_to_provisions(agent_type: str) -> None:
    # tower_job_log has no provision_uuid (db_describe_table, staging q13), so the
    # 429 fallback pointed sub-agents at a table that cannot be tied to a provision.
    prompt = get_agent_prompt(agent_type)
    fallback = _flat(prompt[prompt.index("**AAP2 quota exceeded (429):**") :].split("\n- ")[0])
    assert "(`provision_job` for each action's job" in fallback
    assert "direct database queries (`tower_job_log`" not in fallback


def test_pitfalls_say_tower_job_id_is_the_provision_job_only() -> None:
    # tower_job_id matched the provision-action job 1274/1274 times and a
    # stop/start/destroy job 0/1633 times; a provision also has many stop/start runs.
    pitfalls = get_agent_prompt("orchestrator")
    pitfalls = _flat(pitfalls[pitfalls.index("## Provision Database: Column Pitfalls") :])
    assert "`tower_job_url` is the `provision` action's job only" in pitfalls
    assert "one row per action run: filter on `action`" in pitfalls
    assert "one row per `action`" not in pitfalls
