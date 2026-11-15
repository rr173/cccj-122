"""SQL gate tests: the approved subset is accepted, everything else fails."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cq_helpers as cq
from controlled_query import UnsupportedQueryError, parse_catalog
from controlled_query.sqlcheck import check_query

CATALOG = parse_catalog(cq.catalog_document())


class ApprovedSyntaxTest(unittest.TestCase):
    def check(self, sql, relations=None):
        checked = check_query(sql, CATALOG)
        if relations is not None:
            self.assertEqual(checked.relations, relations)
        return checked

    def test_plain_select(self):
        self.check('SELECT "fact id", concept FROM fact_lines WHERE concept = \'rev\'',
                   ["fact_lines"])

    def test_quoted_identifiers_with_spaces(self):
        self.check('SELECT f."fact id" FROM fact_lines AS f ORDER BY f."fact id"'
                   ' LIMIT 10 OFFSET 2', ["fact_lines"])

    def test_cte_and_chained_cte(self):
        self.check(
            "WITH a AS (SELECT concept FROM fact_lines),"
            " b AS (SELECT concept FROM a) SELECT * FROM b",
            ["fact_lines"])

    def test_cte_may_shadow_physical_table_name(self):
        # "facts" is a physical table, but here it is only a CTE name whose
        # body reads the public relation; the reference resolves to the CTE.
        self.check(
            "WITH facts AS (SELECT concept FROM fact_lines) SELECT * FROM facts",
            ["fact_lines"])

    def test_cte_may_shadow_public_relation_name(self):
        self.check(
            "WITH fact_lines AS (SELECT concept FROM shipment_lines)"
            " SELECT * FROM fact_lines",
            ["shipment_lines"])

    def test_alias_may_equal_physical_table_name(self):
        self.check("SELECT facts.concept FROM fact_lines AS facts",
                   ["fact_lines"])

    def test_derived_table(self):
        self.check("SELECT x.concept FROM (SELECT concept FROM fact_lines) AS x",
                   ["fact_lines"])

    def test_correlated_exists(self):
        self.check(
            "SELECT \"ship id\" FROM shipment_lines s"
            " WHERE EXISTS (SELECT 1 FROM fact_lines f"
            "              WHERE f.marker = s.marker)",
            ["fact_lines", "shipment_lines"])

    def test_not_exists_and_in_subquery(self):
        self.check(
            "SELECT concept FROM fact_lines f"
            " WHERE NOT EXISTS (SELECT 1 FROM shipment_lines s"
            "                  WHERE s.marker = f.marker)")
        self.check(
            "SELECT concept FROM fact_lines"
            " WHERE marker IN (SELECT marker FROM shipment_lines)")

    def test_joins(self):
        self.check("SELECT * FROM fact_lines f INNER JOIN shipment_lines s"
                   " ON f.marker = s.marker")
        self.check("SELECT * FROM fact_lines f LEFT JOIN shipment_lines s"
                   " ON f.marker = s.marker")
        self.check("SELECT * FROM fact_lines f LEFT OUTER JOIN shipment_lines s"
                   " ON f.marker = s.marker")
        self.check("SELECT * FROM fact_lines CROSS JOIN shipment_lines")
        self.check("SELECT * FROM fact_lines, shipment_lines")

    def test_group_having_aggregates(self):
        self.check(
            "SELECT marker, COUNT(*) AS n, COUNT(amount) AS na, SUM(amount) AS s,"
            " AVG(amount) AS a, MIN(amount) AS lo, MAX(amount) AS hi"
            " FROM shipment_lines GROUP BY marker HAVING COUNT(*) > 1")

    def test_union_all_with_order_limit(self):
        self.check(
            "SELECT concept AS c FROM fact_lines UNION ALL"
            " SELECT note FROM shipment_lines ORDER BY c LIMIT 5 OFFSET 1")
        # parenthesized arms are plain grouping
        self.check(
            "(SELECT concept AS c FROM fact_lines) UNION ALL"
            " (SELECT note FROM shipment_lines)")

    def test_scalar_functions_and_case_cast(self):
        self.check(
            "SELECT COALESCE(note, 'n/a'), NULLIF(note, 'x'), ABS(amount),"
            " LENGTH(note), SUBSTR(note, 1, 2), SUBSTR(note, 2),"
            " TYPEOF(payload),"
            " CASE WHEN amount > 1 THEN 'big' ELSE 'small' END,"
            " CASE note WHEN 'ok' THEN 1 ELSE 0 END,"
            " CAST(amount AS TEXT), CAST(note AS INTEGER)"
            " FROM shipment_lines")

    def test_comparisons_arithmetic_like(self):
        self.check(
            "SELECT * FROM shipment_lines"
            " WHERE amount BETWEEN 1 AND 10 AND note IS NOT NULL"
            " AND note LIKE 'o%' ESCAPE '!' AND amount % 7 <> 3"
            " AND amount + 1 * 2 - 3 / 4 > 0 AND note || '!' <> ''"
            " AND -amount < 0 AND note IN ('ok', 'big')")

    def test_cte_column_name_list(self):
        self.check(
            "WITH c(the_concept, the_marker) AS"
            " (SELECT concept, marker FROM fact_lines)"
            " SELECT the_concept FROM c",
            ["fact_lines"])

    def test_self_join(self):
        self.check(
            "SELECT a.concept FROM fact_lines a"
            " JOIN fact_lines b ON b.marker = a.marker",
            ["fact_lines"])

    def test_collate_and_literals(self):
        self.check(
            "SELECT x'00ff', 9223372036854775807, 1.5, 't', NULL, TRUE"
            " FROM shipment_lines WHERE note COLLATE NOCASE = 'OK'")

    def test_ifnull_and_iif_are_approved_aliases(self):
        # SQLGlot normalizes IFNULL -> COALESCE and IIF -> CASE, which are
        # approved constructs with identical SQLite semantics, so the
        # spellings are accepted and documented.
        self.check("SELECT IFNULL(note, 'x') FROM shipment_lines")
        self.check("SELECT IIF(amount > 1, 'big', 'small') FROM shipment_lines")


class RejectedSyntaxTest(unittest.TestCase):
    def expect_reject(self, sql, needle=""):
        with self.assertRaises(UnsupportedQueryError) as ctx:
            check_query(sql, CATALOG)
        if needle:
            self.assertIn(needle, str(ctx.exception))

    def test_physical_table_not_visible(self):
        self.expect_reject("SELECT * FROM facts", "not a public relation")
        self.expect_reject("SELECT * FROM contexts", "not a public relation")
        self.expect_reject("SELECT * FROM shipments", "not a public relation")

    def test_schema_qualified_names_rejected(self):
        self.expect_reject("SELECT * FROM main.fact_lines", "schema-qualified")
        self.expect_reject("SELECT * FROM main.facts", "schema-qualified")

    def test_metadata_tables_not_visible(self):
        self.expect_reject("SELECT * FROM sqlite_master", "not a public relation")
        self.expect_reject("SELECT * FROM sqlite_schema", "not a public relation")

    def test_multi_statement_rejected(self):
        self.expect_reject("SELECT 1; SELECT 2", "exactly one statement")
        self.expect_reject("SELECT 1; DROP TABLE facts", "exactly one statement")

    def test_non_select_statements_rejected(self):
        self.expect_reject("PRAGMA table_info(facts)")
        self.expect_reject("ATTACH DATABASE 'x' AS y")
        self.expect_reject("INSERT INTO fact_lines VALUES (1)")
        self.expect_reject("UPDATE fact_lines SET concept = 'x'")
        self.expect_reject("DELETE FROM fact_lines")
        self.expect_reject("DROP TABLE fact_lines")
        self.expect_reject("CREATE TABLE t(a)")
        self.expect_reject("VACUUM")

    def test_table_valued_functions_rejected(self):
        self.expect_reject("SELECT * FROM json_each('[1,2]')", "table-valued")
        self.expect_reject("SELECT * FROM pragma_table_info('facts')",
                           "table-valued")

    def test_unapproved_functions_rejected(self):
        for fn, needle in (
                ("random()", "RAND"), ("hex(note)", "HEX"),
                ("upper(note)", "UPPER"), ("printf('%d', amount)", "PRINTF"),
                ("load_extension('/tmp/x')", "LOAD_EXTENSION"),
                ("changes()", "CHANGES"), ("sqlite_version()", "VERSION"),
                ("date('now')", "DATE"), ("instr(note, 'k')", "POSITION"),
                ("replace(note,'a','b')", "REPLACE"),
                ("round(1.5)", "ROUND")):
            self.expect_reject(f"SELECT {fn} FROM shipment_lines", needle)

    def test_distinct_rejected(self):
        self.expect_reject("SELECT DISTINCT concept FROM fact_lines", "DISTINCT")
        self.expect_reject("SELECT COUNT(DISTINCT concept) FROM fact_lines")

    def test_window_and_filter_rejected(self):
        self.expect_reject("SELECT ROW_NUMBER() OVER () FROM fact_lines")
        self.expect_reject(
            "SELECT SUM(amount) FILTER (WHERE amount > 1) FROM shipment_lines")

    def test_recursive_cte_rejected(self):
        self.expect_reject(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c"
            " WHERE x < 3) SELECT * FROM c", "recursive")

    def test_scalar_subquery_rejected(self):
        self.expect_reject(
            "SELECT * FROM shipment_lines"
            " WHERE amount = (SELECT MAX(amount) FROM shipment_lines)",
            "scalar subqueries")

    def test_values_list_rejected(self):
        self.expect_reject("SELECT * FROM (VALUES (1), (2))")

    def test_join_variants_rejected(self):
        self.expect_reject(
            "SELECT * FROM fact_lines f RIGHT JOIN shipment_lines s"
            " ON f.marker = s.marker")
        self.expect_reject(
            "SELECT * FROM fact_lines f FULL JOIN shipment_lines s"
            " ON f.marker = s.marker")
        self.expect_reject(
            "SELECT * FROM fact_lines f NATURAL JOIN shipment_lines s")
        self.expect_reject(
            "SELECT * FROM fact_lines f JOIN shipment_lines s USING (marker)",
            "USING")

    def test_set_operations_rejected(self):
        self.expect_reject("SELECT concept FROM fact_lines UNION"
                           " SELECT note FROM shipment_lines", "UNION ALL")
        self.expect_reject("SELECT concept FROM fact_lines EXCEPT"
                           " SELECT note FROM shipment_lines")
        self.expect_reject("SELECT concept FROM fact_lines INTERSECT"
                           " SELECT note FROM shipment_lines")

    def test_parameters_rejected(self):
        self.expect_reject("SELECT * FROM fact_lines WHERE concept = ?")

    def test_cte_scope_rules(self):
        # a non-recursive CTE never sees itself
        self.expect_reject("WITH c AS (SELECT * FROM c) SELECT * FROM c")
        # a CTE only sees CTEs declared before it
        self.expect_reject(
            "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS x)"
            " SELECT * FROM a")
        # duplicate names at one level are ambiguous
        self.expect_reject(
            "WITH a AS (SELECT 1 AS x), a AS (SELECT 2 AS x) SELECT * FROM a",
            "duplicate CTE")
        # ... but a physical table name inside a CTE body stays physical:
        # the CTE name does not leak into its own body.
        self.expect_reject(
            "WITH facts AS (SELECT * FROM facts) SELECT * FROM facts",
            "not a public relation")

    def test_materialized_hint_rejected(self):
        self.expect_reject(
            "WITH c AS MATERIALIZED (SELECT 1 AS x) SELECT * FROM c")

    def test_nested_cte_cannot_reach_physical_table(self):
        self.expect_reject(
            "WITH outer_q AS (WITH inner_q AS (SELECT * FROM facts)"
            " SELECT * FROM inner_q) SELECT * FROM outer_q",
            "not a public relation")

    def test_exists_and_in_cannot_reach_physical_table(self):
        self.expect_reject(
            "SELECT * FROM fact_lines f"
            " WHERE EXISTS (SELECT 1 FROM facts WHERE facts.id = f.\"fact id\")",
            "not a public relation")
        self.expect_reject(
            "SELECT * FROM fact_lines"
            " WHERE marker IN (SELECT name FROM sqlite_master)",
            "not a public relation")

    def test_cte_body_cannot_use_schema_qualified_names(self):
        self.expect_reject(
            "WITH x AS (SELECT * FROM main.fact_lines) SELECT * FROM x",
            "schema-qualified")

    def test_empty_sql_rejected(self):
        self.expect_reject("", "empty")
        self.expect_reject("   -- only a comment\n", "no statement")


if __name__ == "__main__":
    unittest.main()
