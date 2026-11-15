"""Rejection tests: unsupported SQL, bad base tables, and hidden constructs."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pq_helpers import WarehouseTestCase

from provenance_query import BuildError, UnsupportedQueryError  # noqa: E402

WAREHOUSE = """
CREATE TABLE t(id INTEGER PRIMARY KEY, a, b);
INSERT INTO t VALUES (1, 1, 'x'), (2, 2, 'y');
CREATE TABLE u(id INTEGER PRIMARY KEY, a);
INSERT INTO u VALUES (1, 1);
"""

UNSUPPORTED = [
    ("distinct", "SELECT DISTINCT a FROM t"),
    ("aggregate count", "SELECT count(*) FROM t"),
    ("aggregate sum", "SELECT sum(a) AS s FROM t"),
    ("group by", "SELECT a FROM t GROUP BY a"),
    ("having", "SELECT a FROM t HAVING a"),
    ("window", "SELECT a, row_number() OVER () FROM t"),
    ("order by", "SELECT a FROM t ORDER BY a"),
    ("limit", "SELECT a FROM t LIMIT 1"),
    ("offset", "SELECT a FROM t LIMIT 1 OFFSET 2"),
    ("left join", "SELECT * FROM t LEFT JOIN u ON t.a = u.a"),
    ("right join", "SELECT * FROM t RIGHT JOIN u ON t.a = u.a"),
    ("full join", "SELECT * FROM t FULL JOIN u ON t.a = u.a"),
    ("natural join", "SELECT * FROM t NATURAL JOIN u"),
    ("join using", "SELECT * FROM t JOIN u USING (a)"),
    ("exists", "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u)"),
    ("in subquery", "SELECT a FROM t WHERE a IN (SELECT a FROM u)"),
    ("scalar subquery", "SELECT (SELECT 1) AS x FROM t"),
    ("recursive cte", "WITH RECURSIVE c AS (SELECT 1 AS x UNION ALL SELECT 1) SELECT x FROM c"),
    ("union distinct", "SELECT a FROM t UNION SELECT a FROM u"),
    ("except", "SELECT a FROM t EXCEPT SELECT a FROM u"),
    ("intersect", "SELECT a FROM t INTERSECT SELECT a FROM u"),
    ("table function", "SELECT * FROM json_each('[]')"),
    ("user function", "SELECT abs(a) AS x FROM t"),
    ("delete", "DELETE FROM t"),
    ("insert", "INSERT INTO t VALUES (3, 3, 'z')"),
    ("update", "UPDATE t SET a = 1"),
    ("create table", "CREATE TABLE z(id INTEGER PRIMARY KEY)"),
    ("multiple statements", "SELECT a FROM t; SELECT a FROM u"),
    ("empty file", ""),
    ("values list", "SELECT * FROM (VALUES (1))"),
    ("parameter", "SELECT a FROM t WHERE a = ?"),
    ("materialized cte", "WITH c AS MATERIALIZED (SELECT 1 AS x) SELECT x FROM c"),
    ("compound order by", "SELECT a FROM t UNION ALL SELECT a FROM u ORDER BY 1"),
]

HIDDEN = [
    ("unsupported inside cte",
     "WITH c AS (SELECT DISTINCT a FROM t) SELECT a FROM c"),
    ("unsupported inside derived table",
     "SELECT a FROM (SELECT a FROM t ORDER BY a) AS s"),
    ("aggregate hidden in case",
     "SELECT CASE WHEN a THEN count(*) END AS x FROM t"),
    ("window hidden in projection",
     "SELECT (row_number() OVER ()) AS x FROM t"),
]


class TestRejection(WarehouseTestCase):
    def setUp(self):
        super().setUp()
        self.make_warehouse(WAREHOUSE)

    def test_unsupported_constructs_are_rejected(self):
        for label, sql in UNSUPPORTED:
            with self.subTest(label):
                with self.assertRaises(UnsupportedQueryError):
                    self.build(sql, "r_" + label.replace(" ", "_") + ".sqlite")

    def test_hidden_unsupported_constructs_are_rejected(self):
        for label, sql in HIDDEN:
            with self.subTest(label):
                with self.assertRaises(UnsupportedQueryError):
                    self.build(sql, "h_" + label.replace(" ", "_") + ".sqlite")

    def test_failed_build_leaves_no_result_file(self):
        for label, sql in UNSUPPORTED[:5]:
            path = os.path.join(
                self.result_dir, "leftover_" + label.replace(" ", "_") + ".sqlite"
            )
            with self.assertRaises(UnsupportedQueryError):
                sql_path = self.write_sql(sql)
                from provenance_query import ProvenanceQueryBuilder

                ProvenanceQueryBuilder(self.db_path).build(sql_path, path)
            self.assertFalse(os.path.exists(path), label)

    def test_ambiguous_unqualified_column_is_rejected(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x);
        CREATE TABLE b(id INTEGER PRIMARY KEY, x);
        INSERT INTO a VALUES (1, 1);
        INSERT INTO b VALUES (1, 2);
        """)
        with self.assertRaises(BuildError) as ctx:
            self.build("SELECT x AS v FROM a JOIN b ON a.id = b.id")
        self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_unqualified_column_inside_derived_table_reports_ambiguity(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x);
        CREATE TABLE b(id INTEGER PRIMARY KEY, x);
        INSERT INTO a VALUES (1, 1);
        INSERT INTO b VALUES (1, 2);
        """)
        with self.assertRaises(BuildError) as ctx:
            self.build(
                "SELECT s.v AS v FROM (SELECT x AS v FROM a JOIN b ON a.id=b.id) AS s"
            )
        self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_cte_backward_and_forward_reference(self):
        # A CTE declared later must not be visible to an earlier CTE body.
        with self.assertRaises(BuildError):
            self.build(
                "WITH a AS (SELECT x AS x FROM later),"
                " later AS (SELECT 1 AS x) SELECT x FROM a"
            )
        # A CTE cannot reference itself (non-recursive, declared-order visibility).
        with self.assertRaises(BuildError):
            self.build("WITH a AS (SELECT x FROM a) SELECT x FROM a")

    def test_bad_primary_key_cases(self):
        cases = {
            "no primary key": """
                CREATE TABLE bad(id INTEGER, v);
                INSERT INTO bad VALUES (1, 'x');
            """,
            "null in text primary key": """
                CREATE TABLE bad(k TEXT PRIMARY KEY, v);
                INSERT INTO bad VALUES (NULL, 'x');
            """,
            "composite pk with null": """
                CREATE TABLE bad(a TEXT, b TEXT, v, PRIMARY KEY(a, b));
                INSERT INTO bad VALUES ('x', NULL, 'v');
            """,
        }
        for label, script in cases.items():
            with self.subTest(label):
                path = os.path.join(self.tmp, "bad_" + label.replace(" ", "_") + ".sqlite")
                conn = sqlite3_connect(path)
                conn.executescript(script)
                conn.commit()
                conn.close()
                sql_path = self.write_sql("SELECT * FROM bad")
                from provenance_query import ProvenanceQueryBuilder

                result = os.path.join(
                    self.result_dir, "bad_" + label.replace(" ", "_") + ".out"
                )
                with self.assertRaises(BuildError):
                    ProvenanceQueryBuilder(path).build(sql_path, result)
                self.assertFalse(os.path.exists(result))

    def test_mixed_affinity_union_all_is_rejected(self):
        # SQLite documents that a compound SELECT column whose branches have
        # different affinities has an indeterminate affinity; we refuse to
        # guess a rule SQLite itself leaves undefined.
        self.make_warehouse("""
        CREATE TABLE n(id INTEGER PRIMARY KEY, x NUMERIC);
        CREATE TABLE t(id INTEGER PRIMARY KEY, x TEXT);
        INSERT INTO n VALUES (1, 7);
        INSERT INTO t VALUES (1, '09');
        """)
        with self.assertRaises(UnsupportedQueryError) as ctx:
            self.build(
                "SELECT x AS x FROM (SELECT n.x AS x FROM n"
                " UNION ALL SELECT t.x AS x FROM t)"
            )
        self.assertIn("indeterminate", str(ctx.exception))
        # Same-affinity branches are fine.
        self.make_warehouse("""
        CREATE TABLE m(id INTEGER PRIMARY KEY, x NUMERIC);
        INSERT INTO m VALUES (1, 8);
        """)
        result = self.build(
            "SELECT x AS x FROM (SELECT m.x AS x FROM m"
            " UNION ALL SELECT m.x AS x FROM m)",
            "same_affinity.sqlite",
        )
        reader = self.reader(result)
        # One distinct value 8 with multiplicity 2 (both branches contributed).
        self.assertEqual(len(reader.rows()), 1)
        self.assertEqual(reader.rows()[0].multiplicity, 2)

    def test_views_and_virtual_tables_are_not_base_tables(self):
        self.make_warehouse("""
        CREATE TABLE base(id INTEGER PRIMARY KEY, x);
        INSERT INTO base VALUES (1, 1);
        CREATE VIEW v AS SELECT * FROM base;
        """)
        with self.assertRaises(UnsupportedQueryError):
            self.build("SELECT * FROM v")


def sqlite3_connect(path):
    import sqlite3

    return sqlite3.connect(path)


if __name__ == "__main__":
    unittest.main()
