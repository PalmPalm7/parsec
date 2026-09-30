"""A failed SQL query must come back as an error, not as a successful result.

The Reporting MCP returns a failed query as ordinary text ("Query error: ...").
On the live pods dev q03 got an UndefinedColumnError and a statement timeout
back as ``{"result": "Query error: ..."}``: the bridge marked both calls as
successes, ToolStats counted 0 errors instead of 2, and the model was given no
hint about what to do next.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import src.agent.parsec_mcp as bridge
from src.tools.provision_db import execute_query

_SQL = "SELECT p.user_email FROM provisions p WHERE p.babylon_guid = 'ctbz4'"

# Verbatim shape of the live Reporting-MCP text (dev q03).
_UNDEFINED_COLUMN = (
    "Query error: (sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) "
    "<class 'asyncpg.exceptions.UndefinedColumnError'>: column p.user_email does not exist\n"
    "[SQL: SELECT p.uuid, p.user_email\nFROM provisions p\nWHERE p.babylon_guid = 'ctbz4']\n"
    "(Background on this error at: https://sqlalche.me/e/20/f405)"
)
_TIMEOUT = (
    "Query error: (sqlalchemy.dialects.postgresql.asyncpg.Error) "
    "<class 'asyncpg.exceptions.QueryCanceledError'>: canceling statement due to "
    "statement timeout\n[SQL: SELECT 1\nFROM provisions p]\n"
    "(Background on this error at: https://sqlalche.me/e/20/dbapi)"
)

# Verbatim live text (staging q13, first failed query_provisions_db call). Postgres's
# HINT names the real column; the model's next call quoted it and succeeded.
_UNDEFINED_COLUMN_WITH_HINT = (
    "Query error: (sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) <class "
    "'asyncpg.exceptions.UndefinedColumnError'>: column tl.deployerjob does not exist\n"
    'HINT:  Perhaps you meant to reference the column "tl.deployerJob".\n'
    "[SQL: \nSELECT \n    tl.deployerJob,\n    tl.towerHost,\n    tl.towerJobURL,\n"
    "    tl.comments,\n    tl.created_at\nFROM tower_job_log tl\n"
    "WHERE tl.created_at >= '2026-09-23'::timestamp\nORDER BY tl.created_at DESC\n"
    "LIMIT 20\n]\n(Background on this error at: https://sqlalche.me/e/20/f405)"
)


def _run(text: str) -> dict:
    with patch("src.connections.reporting_mcp.call_tool", AsyncMock(return_value={"result": text})):
        return asyncio.run(execute_query(_SQL))


def test_undefined_column_is_an_error_with_a_hint():
    result = _run(_UNDEFINED_COLUMN)
    assert "result" not in result
    assert result["error"].startswith("Query error:")
    assert "column p.user_email does not exist" in result["error"]
    # The SQL echo and the docs link are noise the model already has.
    assert "[SQL:" not in result["error"] and "sqlalche.me" not in result["error"]
    assert "db_describe_table" in result["hint"]


def test_undefined_column_hint_states_what_ordered_by_really_holds():
    # The live rows hold the requester's email (dev q03/q04/q13, staging q10), or NULL.
    hint = _run(_UNDEFINED_COLUMN)["hint"]
    assert (
        "p.ordered_by is the requester's email (FK to users.email; may be NULL). "
        "p.user_id → users is the assigned user and can differ."
    ) in hint
    assert "requester's name" not in hint


def test_undefined_column_hint_says_mixed_case_columns_need_quotes():
    # tower_job_log and provision_job have camelCase columns; unquoted, tl.deployerJob
    # is read as tl.deployerjob and fails (staging q13, twice).
    hint = _run(_UNDEFINED_COLUMN_WITH_HINT)["hint"]
    assert 'tl."deployerJob"' in hint and "HINT" in hint


def test_postgres_hint_line_survives_and_the_sql_echo_does_not():
    result = _run(_UNDEFINED_COLUMN_WITH_HINT)
    assert result["error"] == (
        "Query error: (sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) <class "
        "'asyncpg.exceptions.UndefinedColumnError'>: column tl.deployerjob does not exist\n"
        'HINT:  Perhaps you meant to reference the column "tl.deployerJob".'
    )
    assert 'Perhaps you meant to reference the column "tl.deployerJob"' in result["error"]


def test_detail_lines_are_kept_and_the_parameters_echo_is_dropped():
    text = (
        "Query error: (sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) <class "
        "'asyncpg.exceptions.UndefinedFunctionError'>: operator does not exist: text = integer\n"
        "DETAIL:  No operator matches the given name and argument types.\n"
        "HINT:  You might need to add explicit type casts.\n"
        "[SQL: SELECT 1 FROM provisions p WHERE p.babylon_guid = 5]\n"
        "[parameters: ()]\n"
        "(Background on this error at: https://sqlalche.me/e/20/f405)"
    )
    error = _run(text)["error"]
    assert error.splitlines()[1:] == [
        "DETAIL:  No operator matches the given name and argument types.",
        "HINT:  You might need to add explicit type casts.",
    ]


def test_docs_link_is_dropped_when_there_is_no_sql_echo():
    text = (
        "Query error: connection was closed\n"
        "(Background on this error at: https://sqlalche.me/e/20/e3q8)"
    )
    assert _run(text) == {"error": "Query error: connection was closed"}


def test_statement_timeout_is_an_error_with_a_hint():
    result = _run(_TIMEOUT)
    assert "statement timeout" in result["error"]
    assert "babylon_guid" in result["hint"] and "ILIKE" in result["hint"]


def test_other_query_errors_have_no_hint():
    result = _run('Query error: syntax error at or near "FORM"\n[SQL: SELECT 1 FORM t]')
    assert result == {"error": 'Query error: syntax error at or near "FORM"'}


def test_a_successful_query_is_unchanged():
    result = _run("| a |\n| --- |\n| 1 |\n\n1 row returned")
    assert result == {"result": "| a |\n| --- |\n| 1 |\n\n1 row returned", "row_count": 1}


@pytest.mark.parametrize("text", [_UNDEFINED_COLUMN, _TIMEOUT], ids=["undefined_column", "timeout"])
def test_bridge_counts_a_query_error_as_a_failed_call(text):
    handler = bridge._make_handler("query_provisions_db", allow_writes=False)
    stats = bridge.ToolStats()
    token = bridge.tool_stats.set(stats)
    try:
        with patch(
            "src.connections.reporting_mcp.call_tool", AsyncMock(return_value={"result": text})
        ):
            out = asyncio.run(handler({"sql": _SQL}))
    finally:
        bridge.tool_stats.reset(token)
    assert out["is_error"] is True
    assert (stats.calls, stats.errors) == (1, 1)
