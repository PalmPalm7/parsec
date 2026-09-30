"""Tool: query_provisions_db — execute read-only SQL via the Reporting MCP.

All database access goes through the Reporting MCP server. Schema discovery,
domain knowledge, and investigation prompts are exposed as dynamically
discovered MCP tools (see src/connections/reporting_mcp.py).
"""

import logging
import re

from src.config import get_config

logger = logging.getLogger(__name__)

_FORBIDDEN_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE|"
    r"DO|CALL|SET|RESET|DISCARD|LOAD|VACUUM|ANALYZE|CLUSTER|REINDEX|LOCK|"
    r"PREPARE|DEALLOCATE|LISTEN|NOTIFY|UNLISTEN)\b",
    re.IGNORECASE,
)

_ROW_COUNT_PATTERN = re.compile(r"(\d+) rows? returned")

#: The Reporting MCP returns a failed query as an ordinary text result starting
#: with this prefix, not as an MCP error, so it arrived as ``{"result": ...}``.
#: The model, ToolStats and the metrics all read that as a successful call.
_QUERY_ERROR_PREFIX = "Query error:"

#: What to do next for the database errors the agents actually hit, keyed by the
#: asyncpg exception class named in the error text.
_QUERY_ERROR_HINTS = {
    "UndefinedColumnError": (
        "A column in this query does not exist. Call db_describe_table on the table "
        "before retrying instead of guessing column names. provisions has no user_email "
        "or email column: JOIN users u ON u.id = p.user_id for the email; the "
        "requester's name is p.ordered_by."
    ),
    "QueryCanceledError": (
        "The query hit the database statement timeout. Run the indexed exact match "
        "alone first (e.g. WHERE p.babylon_guid = '<guid>') and only then add "
        "leading-wildcard ILIKE '%...%' or OR-ed conditions, with a date range."
    ),
}

#: String literals, quoted identifiers and comments, matched left to right the
#: way the SQL lexer would. None of them can contain executable SQL, so they are
#: blanked before the keyword and statement checks.
#:
#: The keyword check used to scan the raw text, so a catalog item named
#: ``...-cluster`` or ``... (Cluster)`` in a WHERE clause read as the CLUSTER
#: command and the query was refused — and RHDP catalog names are full of
#: "cluster". Postgres only ever makes a literal or comment *longer* than these
#: patterns do (E'' backslash escapes, nested /* */), never shorter, so any
#: disagreement leaves more text to be checked as code: it fails closed.
_NON_CODE_PATTERN = re.compile(
    r"'(?:[^']|'')*'"  # string literal, '' as an escaped quote
    r'|"(?:[^"]|"")*"'  # quoted identifier
    r"|--[^\n]*"  # line comment
    r"|/\*.*?\*/",  # block comment
    re.DOTALL,
)


def _code_only(sql: str) -> str:
    """``sql`` with literals, quoted identifiers and comments replaced by a space."""
    return _NON_CODE_PATTERN.sub(" ", sql)


def validate_sql(sql: str) -> str | None:
    """Validate that SQL is a read-only SELECT. Returns error message or None."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return "Empty SQL statement"

    first_word = stripped.split()[0].upper()
    if first_word not in ("SELECT", "WITH"):
        return f"Only SELECT queries allowed, got: {first_word}"

    code = _code_only(stripped)
    match = _FORBIDDEN_PATTERN.search(code)
    if match:
        return f"Forbidden SQL keyword: {match.group()}"

    if ";" in code:
        return "Multiple statements not allowed"

    return None


def _query_error(text: str) -> dict:
    """Turn a Reporting-MCP ``Query error: ...`` text into an ``{"error"}`` result.

    Keeps only the first line: the rest is SQLAlchemy's echo of the SQL the model
    just wrote and a link to its docs.
    """
    message = text.strip().splitlines()[0]
    error: dict[str, str] = {"error": message}
    for exc_name, hint in _QUERY_ERROR_HINTS.items():
        if exc_name in message:
            error["hint"] = hint
            break
    return error


async def execute_query(sql: str) -> dict:
    """Execute a read-only SQL query via Reporting MCP."""
    error = validate_sql(sql)
    if error:
        return {"error": error}

    cfg = get_config()
    max_rows = cfg.provision_db.get("max_rows", 500)

    from src.connections.reporting_mcp import call_tool

    result = await call_tool(
        "query",
        {
            "sql": sql,
            "limit": max_rows,
            "output_format": "markdown",
        },
    )

    text = result.get("result", "")
    if isinstance(text, str) and text.lstrip().startswith(_QUERY_ERROR_PREFIX):
        return _query_error(text)

    if "error" not in result:
        match = _ROW_COUNT_PATTERN.search(result.get("result", ""))
        if match:
            result["row_count"] = int(match.group(1))

    return result
