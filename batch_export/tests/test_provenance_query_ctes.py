"""CTE/derived-table rules: column lists, scoping, ordering visibility."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pq_helpers import WarehouseTestCase

from provenance_query import BuildError, UnsupportedQueryError  # noqa: E402


WAREHOUSE = """
CREATE TABLE t(id INTEGER PRIMARY KEY, a, b);
INSERT INTO t VALUES (1, 10, 20), (2, 30, 40);
"""


class TestCteColumnLists(WarehouseTestCase):
    def setUp(self):
        super().setUp()
        self.make_warehouse(WAREHOUSE)

    def test_rename_by_position(self):
        result = self.build("WITH c(x, y) AS (SELECT a, b FROM t) SELECT x, y FROM c")
        reader = self.reader(result)
        self.assertEqual(reader.column_names(), ["x", "y"])
        self.assert_multisets_equal(
            self.result_multiset(reader),
            self.direct_multiset("SELECT a AS x, b AS y FROM t"),
        )

    def test_column_count_must_match(self):
        with self.assertRaises(BuildError) as ctx:
            self.build("WITH c(x) AS (SELECT a, b FROM t) SELECT x FROM c")
        self.assertIn("declares 1", str(ctx.exception))
        self.assertIn("produces 2", str(ctx.exception))

    def test_derived_table_column_list_must_match(self):
        with self.assertRaises(BuildError):
            self.build("SELECT * FROM (SELECT a, b FROM t) AS s(only_one)")

    def test_qualified_reference_uses_renamed_column(self):
        # The renamed column must be addressable; the original name must not
        # exist on the CTE any more.
        result = self.build(
            "WITH c(renamed) AS (SELECT a FROM t) SELECT c.renamed AS r FROM c"
        )
        reader = self.reader(result)
        self.assertEqual(reader.column_names(), ["r"])
        with self.assertRaises(BuildError):
            self.build(
                "WITH c(renamed) AS (SELECT a FROM t) SELECT c.a AS r FROM c",
                "case2.sqlite",
            )


class TestCteDeclarationOrder(WarehouseTestCase):
    def setUp(self):
        super().setUp()
        self.make_warehouse(WAREHOUSE)

    def test_chained_ctes_visible_in_order(self):
        result = self.build(
            "WITH first AS (SELECT a AS x FROM t),"
            " second AS (SELECT x AS y FROM first) "
            "SELECT y FROM second"
        )
        reader = self.reader(result)
        self.assertEqual(
            self.result_multiset(reader), self.direct_multiset("SELECT a AS y FROM t")
        )

    def test_forward_reference_is_an_error(self):
        with self.assertRaises(BuildError):
            self.build(
                "WITH first AS (SELECT x FROM second),"
                " second AS (SELECT a AS x FROM t) SELECT x FROM first"
            )

    def test_self_reference_is_an_error(self):
        with self.assertRaises(BuildError):
            self.build("WITH c AS (SELECT * FROM c) SELECT * FROM c")

    def test_recursive_keyword_is_rejected(self):
        with self.assertRaises(UnsupportedQueryError):
            self.build(
                "WITH RECURSIVE c AS (SELECT 1 AS x UNION ALL SELECT x FROM c)"
                " SELECT x FROM c"
            )

    def test_same_cte_used_twice_shares_tokens(self):
        result = self.build(
            "WITH c AS (SELECT 1 AS one FROM t) "
            "SELECT x.one AS one FROM c AS x CROSS JOIN c AS y"
        )
        reader = self.reader(result)
        self.assertEqual(reader.rows()[0].multiplicity, 4)
        # Excluding one base row yields 1 (the token is squared, not deduplicated).
        outcome = self.engine_exclusion_multiset(result, ["t(id=1)"])
        self.assertEqual(sum(outcome.values()), 1)

    def test_cte_name_case_folding_matches_sqlite(self):
        # SQLite resolves CTE references case-insensitively (ASCII only).
        result = self.build(
            "WITH Cte AS (SELECT a AS v FROM t) SELECT v AS v FROM cTE"
        )
        reader = self.reader(result)
        self.assertEqual(
            {tuple(x.key() for x in row.values) for row in reader.rows()},
            {("i:10",), ("i:30",)},
        )

    def test_cte_and_base_table_name_collision_prefers_cte(self):
        # A CTE shadows a same-named base table in its scope.
        self.make_warehouse("""
        CREATE TABLE t(id INTEGER PRIMARY KEY, a, b);
        INSERT INTO t VALUES (1, 10, 20), (2, 30, 40);
        CREATE TABLE c(id INTEGER PRIMARY KEY, v);
        INSERT INTO c VALUES (1, 'base');
        """)
        result = self.build(
            "WITH c AS (SELECT 1 AS v FROM t) SELECT v AS v FROM c"
        )
        reader = self.reader(result)
        self.assertEqual(
            {tuple(x.key() for x in row.values) for row in reader.rows()},
            {("i:1",)},
        )


if __name__ == "__main__":
    unittest.main()
