"""The read-only SQL guard must look at code, not at the text inside literals.

On the live pods an AAP2 investigation had five queries refused as
"Forbidden SQL keyword: cluster" — every hit was inside a string literal such
as ci.name IN ('...-troubleshooting-cluster', ...). RHDP catalog names are full
of "cluster", so any provisioning question about an OCP cluster item failed.
"""

from __future__ import annotations

import pytest

from src.tools.provision_db import validate_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM provisions p JOIN catalog_items ci ON ci.id = p.catalog_id "
        "WHERE ci.name LIKE '%ocp-cluster-cnv%'",
        "SELECT 1 FROM t WHERE ci.display_name IN ('OpenShift Troubleshooting (Cluster)')",
        "SELECT 1 FROM t WHERE note = 'update; delete; drop table x'",
        "SELECT 1 FROM t WHERE a = 'it''s a cluster; really'",
        'SELECT "Set" FROM t',
        "SELECT 1 -- we do not ANALYZE here\nFROM t",
        "SELECT 1 /* no DELETE; either */ FROM t",
        "WITH x AS (SELECT 'load balancer' AS kind) SELECT * FROM x",
        "SELECT 1;",
    ],
)
def test_keywords_inside_literals_and_comments_are_allowed(sql):
    assert validate_sql(sql) is None


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("SELECT 1; DROP TABLE provisions", "DROP"),
        ("SELECT 'x'; DELETE FROM provisions; --'", "DELETE"),
        ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "DELETE"),
        ("SELECT 1 FROM t; SELECT 2", "Multiple statements"),
        ("SELECT pg_sleep(1) FROM t CLUSTER", "CLUSTER"),
        # Postgres reads E'\' ; ... ' as ONE literal (\' escapes the quote); the
        # guard ends it at the first quote and checks the rest as code, so it
        # refuses a harmless query rather than ever missing a real statement.
        ("SELECT E'\\' ; DROP TABLE t ; SELECT '", "DROP"),
        # Unterminated literal: nothing is blanked, everything is checked.
        ("SELECT 'abc FROM t; DELETE FROM t", "DELETE"),
        # Nested block comment: Postgres keeps all of it as comment; the guard
        # ends it at the first */ and inspects the tail.
        ("SELECT 1 /* a /* b */ DROP TABLE t */ FROM t", "DROP"),
        ("UPDATE t SET a = 1", "Only SELECT"),
    ],
)
def test_real_statements_are_still_refused(sql, why):
    err = validate_sql(sql)
    assert err is not None and why in err
