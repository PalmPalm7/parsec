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

    if "error" not in result:
        match = _ROW_COUNT_PATTERN.search(result.get("result", ""))
        if match:
            result["row_count"] = int(match.group(1))

    return result
