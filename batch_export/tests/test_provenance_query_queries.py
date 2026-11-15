"""Semantics tests: SQL constructs, typing, affinity, NULLs, structure."""
from __future__ import annotations

import os
import sqlite3
import unittest

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pq_helpers import WarehouseTestCase

MULTI = """
CREATE TABLE L(id INTEGER PRIMARY KEY, v TEXT);
CREATE TABLE R(id INTEGER PRIMARY KEY, v TEXT);
INSERT INTO L VALUES (1,'a1'),(2,'a2');
INSERT INTO R VALUES (1,'b1'),(2,'b2'),(3,'b3');
CREATE TABLE nums(id INTEGER PRIMARY KEY, n NUMERIC, t TEXT, b BLOB, r REAL);
INSERT INTO nums VALUES
  (1, 5, '5', x'35', 5.0),
  (2, 5, '5', x'35', 5.0),
  (3, NULL, NULL, NULL, NULL);
"""


class TestNameAndProjectionRules(WarehouseTestCase):
    def test_star_expansion_and_union_positional_names(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x, y);
        CREATE TABLE b(p INTEGER PRIMARY KEY, q, r);
        INSERT INTO a VALUES (1, 'ax', 'ay');
        INSERT INTO b VALUES (1, 'bp', 'bq');
        """)
        sql = "SELECT * FROM a UNION ALL SELECT * FROM b"
        result = self.build(sql)
        reader = self.reader(result)
        self.assertEqual(reader.column_names(), ["id", "x", "y"])
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(sql)
        )

    def test_union_all_keeps_first_branch_names(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x);
        CREATE TABLE b(id INTEGER PRIMARY KEY, y);
        INSERT INTO a VALUES (1, 1);
        INSERT INTO b VALUES (1, 2);
        """)
        result = self.build(
            "SELECT x AS alpha FROM a UNION ALL SELECT y AS beta FROM b"
        )
        reader = self.reader(result)
        self.assertEqual(reader.column_names(), ["alpha"])
        self.assertEqual(len(reader.rows()), 2)

    def test_qualified_star(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x, y);
        CREATE TABLE b(id INTEGER PRIMARY KEY, p, q);
        INSERT INTO a VALUES (1, '1', '2');
        INSERT INTO b VALUES (1, '3', '4');
        """)
        result = self.build(
            "SELECT a.*, b.q AS bq FROM a JOIN b ON a.id = b.id"
        )
        reader = self.reader(result)
        self.assertEqual(reader.column_names(), ["id", "x", "y", "bq"])

    def test_duplicate_output_name_rejected(self):
        self.make_warehouse("CREATE TABLE a(id INTEGER PRIMARY KEY, x); INSERT INTO a VALUES (1, 1);")
        with self.assertRaises(Exception):
            self.build("SELECT x AS z, x AS z FROM a")

    def test_unnamed_expression_rejected(self):
        self.make_warehouse("CREATE TABLE a(id INTEGER PRIMARY KEY, x); INSERT INTO a VALUES (1, 1);")
        with self.assertRaises(Exception):
            self.build("SELECT x + 1 FROM a")

    def test_unqualified_ambiguity_rejected(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x);
        CREATE TABLE b(id INTEGER PRIMARY KEY, x);
        INSERT INTO a VALUES (1, 1);
        INSERT INTO b VALUES (1, 2);
        """)
        with self.assertRaises(Exception) as ctx:
            self.build("SELECT x AS z FROM a JOIN b ON a.id = b.id")
        self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_qualified_reference_does_not_cross_scope(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x);
        INSERT INTO a VALUES (1, 7);
        """)
        with self.assertRaises(Exception):
            self.build("SELECT s.x AS z FROM (SELECT x AS x FROM a) AS s WHERE a.x = 7")


class TestTypedValues(WarehouseTestCase):
    def test_null_int_real_text_blob_are_distinct(self):
        self.make_warehouse("""
        CREATE TABLE v(id INTEGER PRIMARY KEY, a);
        INSERT INTO v VALUES
          (1, NULL), (2, 1), (3, 1.0), (4, '1'), (5, x'31'),
          (6, ''), (7, x'');
        """)
        sql = "SELECT a AS a FROM v"
        result = self.build(sql)
        reader = self.reader(result)
        kinds = {row.values[0].kind for row in reader.rows()}
        self.assertEqual(kinds, {"n", "i", "f", "t", "b"})
        # Empty text and empty blob must not collapse.
        empties = [row for row in reader.rows()
                   if row.values[0].display() in ("", "x''")]
        self.assertEqual(len(empties), 2)
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(sql)
        )

    def test_integer_precision_survives_json(self):
        self.make_warehouse("""
        CREATE TABLE v(id INTEGER PRIMARY KEY, a);
        INSERT INTO v VALUES (1, 9223372036854775807), (2, -9223372036854775808);
        """)
        sql = "SELECT a AS a FROM v"
        result = self.build(sql)
        reader = self.reader(result)
        multiset = self.result_multiset(reader)
        self.assertIn(("i:9223372036854775807",), multiset)
        self.assertIn(("i:-9223372036854775808",), multiset)

    def test_value_equality_uses_storage_class(self):
        # Two rows with the same value text but distinct storage class must
        # remain two result tuples.
        self.make_warehouse("""
        CREATE TABLE v(id INTEGER PRIMARY KEY, a);
        INSERT INTO v VALUES (1, 5), (2, '5');
        """)
        sql = "SELECT a AS a FROM v"
        result = self.build(sql)
        reader = self.reader(result)
        self.assertEqual(len(reader.rows()), 2)
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(sql)
        )


class TestWhereAndNullSemantics(WarehouseTestCase):
    def test_where_uses_three_valued_logic(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x);
        INSERT INTO a VALUES (1, NULL), (2, 0), (3, 1), (4, 'abc'), (5, '');
        """)
        for predicate in ["a.x", "NOT a.x", "a.x = 1", "a.x IS NULL",
                         "a.x IS NOT NULL", "a.x = 'abc'", "a.x <> 0"]:
            sql = f"SELECT a.id AS id FROM a WHERE {predicate}"
            result = self.build(sql, "r_" + str(abs(hash(predicate))) + ".sqlite")
            reader = self.reader(result)
            self.assert_multisets_equal(
                self.result_multiset(reader), self.direct_multiset(sql),
                f"predicate {predicate}",
            )

    def test_null_join_does_not_match(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x);
        CREATE TABLE b(id INTEGER PRIMARY KEY, y);
        INSERT INTO a VALUES (1, NULL), (2, 'k'), (3, 'z');
        INSERT INTO b VALUES (1, NULL), (2, 'k'), (3, 'z');
        """)
        sql = "SELECT a.id AS aid, b.id AS bid FROM a JOIN b ON a.x = b.y"
        result = self.build(sql)
        reader = self.reader(result)
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(sql)
        )
        self.assertEqual(len(reader.rows()), 2)

    def test_empty_relation_yields_no_tuples(self):
        self.make_warehouse("""
        CREATE TABLE empty_t(id INTEGER PRIMARY KEY, x);
        CREATE TABLE full_t(id INTEGER PRIMARY KEY, y);
        INSERT INTO full_t VALUES (1, 1);
        """)
        result = self.build("SELECT x AS x FROM empty_t")
        reader = self.reader(result)
        self.assertEqual(reader.rows(), [])
        self.assertEqual(reader.meta()["result_rows"], "0")
        # A CROSS JOIN with an empty side is empty but still well-formed.
        result = self.build(
            "SELECT full_t.y AS y FROM empty_t CROSS JOIN full_t", "r2.sqlite"
        )
        reader = self.reader(result)
        self.assertEqual(reader.rows(), [])

    def test_constant_query_without_from(self):
        self.make_warehouse("CREATE TABLE a(id INTEGER PRIMARY KEY, x);")
        for sql, expected in [
            ("SELECT 1 AS one", {("i:1",): 1}),
            ("SELECT 'x' AS s WHERE 1", {("t:x",): 1}),
            ("SELECT 'x' AS s WHERE 0", {}),
        ]:
            result = self.build(sql, "c" + str(abs(hash(sql))) + ".sqlite")
            reader = self.reader(result)
            self.assertEqual(
                {tuple(v.key() for v in row.values): row.multiplicity
                 for row in reader.rows()},
                expected, sql,
            )


class TestAffinityAndCollation(WarehouseTestCase):
    def test_numeric_text_affinity_is_sqlite_exact(self):
        self.make_warehouse("""
        CREATE TABLE n(id INTEGER PRIMARY KEY, x NUMERIC);
        INSERT INTO n VALUES (1, 5), (2, 7);
        CREATE TABLE t(id INTEGER PRIMARY KEY, x TEXT);
        INSERT INTO t VALUES (1, '5'), (2, 'x');
        """)
        cases = [
            "SELECT n.x AS a, n.x = '5' AS eq FROM n",
            "SELECT t.x AS a, t.x = 5 AS eq FROM t",
            "SELECT n.x AS a FROM n WHERE n.x = '5'",
            "SELECT t.x AS a FROM t WHERE t.x = 5",
            "SELECT +n.x AS a, +n.x = '5' AS eq FROM n",
            "SELECT n.x AS a, t.x AS b FROM n JOIN t ON n.x = t.x",
            "SELECT n.x AS a, t.x AS b FROM n JOIN t ON +n.x = t.x",
        ]
        for i, sql in enumerate(cases):
            result = self.build(sql, f"aff{i}.sqlite")
            reader = self.reader(result)
            self.assert_multisets_equal(
                self.result_multiset(reader), self.direct_multiset(sql), sql
            )

    def test_nocase_column_collation(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x TEXT COLLATE NOCASE);
        CREATE TABLE b(id INTEGER PRIMARY KEY, y TEXT);
        INSERT INTO a VALUES (1, 'ABC'), (2, 'def');
        INSERT INTO b VALUES (1, 'abc');
        """)
        for sql in [
            "SELECT a.x AS x FROM a WHERE a.x = 'abc'",
            "SELECT a.x AS x, b.y AS y FROM a JOIN b ON a.x = b.y",
            "SELECT a.x AS x, b.y AS y FROM a JOIN b ON b.y = a.x",
            "SELECT a.x AS x, b.y AS y FROM a JOIN b ON a.x COLLATE BINARY = b.y",
        ]:
            result = self.build(sql, "n" + str(abs(hash(sql))) + ".sqlite")
            reader = self.reader(result)
            self.assert_multisets_equal(
                self.result_multiset(reader), self.direct_multiset(sql), sql
            )

    def test_unary_plus_strips_affinity_only(self):
        self.make_warehouse("""
        CREATE TABLE t(id INTEGER PRIMARY KEY, n NUMERIC);
        INSERT INTO t VALUES (1, 5), (2, 7);
        """)
        cases = [
            "SELECT x AS x FROM (SELECT +t.n AS x FROM t) WHERE x = '5'",
            "SELECT x AS x FROM (SELECT +t.n AS x FROM t) WHERE x = 5",
            "WITH c AS (SELECT +t.n AS x FROM t) SELECT x AS x FROM c WHERE x = '5'",
            "SELECT +t.n AS x FROM t WHERE +t.n = '5'",
            "SELECT t.n AS x FROM t WHERE t.n = '5'",
            # Unary plus keeps the underlying collation for string comparison.
            "SELECT x AS x FROM (SELECT +t.n AS x FROM t) WHERE x = '5'",
        ]
        for i, sql in enumerate(cases):
            result = self.build(sql, f"unary{i}.sqlite")
            reader = self.reader(result)
            self.assert_multisets_equal(
                self.result_multiset(reader), self.direct_multiset(sql), sql
            )

    def test_cast_keeps_written_type(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x TEXT);
        INSERT INTO a VALUES (1, '9'), (2, '1.5'), (3, 'abc');
        """)
        sql = "SELECT CAST(x AS NUMERIC) AS n FROM a"
        result = self.build(sql)
        reader = self.reader(result)
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(sql)
        )

    def test_comparison_and_logic_expressions(self):
        self.make_warehouse("""
        CREATE TABLE a(id INTEGER PRIMARY KEY, x, y);
        INSERT INTO a VALUES (1, 1, 2), (2, 3, 4), (3, NULL, 5);
        """)
        cases = [
            "SELECT a.x AS x FROM a WHERE a.x = 1 OR a.y = 4",
            "SELECT a.x AS x FROM a WHERE NOT a.x = 1",
            "SELECT a.x AS x FROM a WHERE a.x IN (1, 3)",
            "SELECT a.x AS x FROM a WHERE a.x BETWEEN 2 AND 4",
            "SELECT a.x AS x FROM a WHERE a.x IS 1",
            "SELECT a.x AS x FROM a WHERE a.x IS NOT 1",
            "SELECT CASE WHEN a.x > 1 THEN 'big' ELSE 'small' END AS c FROM a",
            "SELECT COALESCE(a.x, -1) AS c FROM a",
            "SELECT NULLIF(a.x, 1) AS c FROM a",
            "SELECT a.x AS x FROM a WHERE 'abc' LIKE 'a%'",
            "SELECT a.x AS x FROM a WHERE 'a%b' LIKE 'a!%b' ESCAPE '!'",
        ]
        for i, sql in enumerate(cases):
            result = self.build(sql, f"e{i}.sqlite")
            reader = self.reader(result)
            self.assert_multisets_equal(
                self.result_multiset(reader), self.direct_multiset(sql), sql
            )


if __name__ == "__main__":
    unittest.main()
