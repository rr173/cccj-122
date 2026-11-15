"""Broad semantics sweep: each supported query shape compared with SQLite.

Every expected multiset comes from the same SQL executed in plain SQLite
(typed in Python); every exclusion expectation comes from a second copy of
the warehouse where the rows are physically deleted.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pq_helpers import WarehouseTestCase

SCHEMA = """
CREATE TABLE a(id INTEGER PRIMARY KEY, k TEXT, v NUMERIC);
CREATE TABLE b(id INTEGER PRIMARY KEY, k TEXT, v NUMERIC);
CREATE TABLE c(id TEXT, n INTEGER, v TEXT, PRIMARY KEY(id, n));
CREATE TABLE "odd name"("id" INTEGER PRIMARY KEY, "a b" TEXT COLLATE NOCASE);
CREATE TABLE empty_side(id INTEGER PRIMARY KEY, x);
CREATE TABLE nulls(id INTEGER PRIMARY KEY, x, y);
INSERT INTO a VALUES (1,'ka',10), (2,'kb',20), (3,'ka',30);
INSERT INTO b VALUES (1,'ka',10), (2,'kb',20), (3,'kc',40);
INSERT INTO c VALUES ('p',1,'c1'), ('p',2,'c2'), ('q',1,'c3');
INSERT INTO "odd name" VALUES (1,'Abc'), (2,'def');
INSERT INTO nulls VALUES (1, NULL, 'n1'), (2, 'x', NULL), (3, 'x', 'n3');
"""

QUERIES = {
    "nested ctes":
        "WITH x AS (SELECT a.k AS k, a.v AS v FROM a), "
        "y AS (SELECT x.k AS k FROM x WHERE x.v > 10) "
        "SELECT y.k AS k FROM y",
    "constant projection duplicate collapsing":
        "WITH x AS (SELECT 1 AS one FROM a) "
        "SELECT x.one AS one FROM x CROSS JOIN x AS x2 UNION ALL "
        "SELECT y.one AS one FROM x AS y",
    "composite primary key":
        "SELECT c.id AS id, c.n AS n FROM c",
    "self join":
        "SELECT a.k AS k, a2.k AS k2 FROM a CROSS JOIN a AS a2",
    "union positional alignment":
        "SELECT a.k AS k, a.v AS v FROM a UNION ALL SELECT b.k AS k, b.v AS v FROM b",
    "identifiers with spaces":
        'SELECT "odd name"."a b" AS x, "odd name"."id" AS id FROM "odd name"',
    "null join":
        "SELECT n.x AS x, m.y AS y FROM nulls AS n JOIN nulls AS m ON n.x = m.y",
    "empty relation":
        "SELECT e.x AS x FROM empty_side AS e",
    "empty join":
        "SELECT a.k AS k FROM a JOIN empty_side AS e ON a.k = e.x",
    "multi-layer cte":
        "WITH l1 AS (SELECT a.k AS k, a.v AS v FROM a),"
        " l2 AS (SELECT l1.k AS k FROM l1 WHERE l1.v = 10),"
        " l3 AS (SELECT l2.k AS k FROM l2 UNION ALL SELECT l2.k FROM l2) "
        "SELECT k FROM l3",
    "derived table":
        "SELECT s.k AS k FROM (SELECT a.k AS k FROM a WHERE a.v > 10) AS s",
    "derived table with column aliases":
        "SELECT s.renamed AS renamed FROM "
        "(SELECT a.k AS renamed, a.v AS value FROM a) AS s",
    "join chain with mixed forms":
        "SELECT a.k AS k, b.v AS bv, c.v AS cv "
        "FROM a JOIN b ON a.k = b.k CROSS JOIN c WHERE c.id = 'p'",
    "three-branch union":
        "SELECT a.k AS k FROM a UNION ALL SELECT b.k FROM b "
        "UNION ALL SELECT c.v FROM c",
    "star with join":
        "SELECT * FROM a CROSS JOIN b",
    "where with boolean operators":
        "SELECT a.k AS k FROM a WHERE (a.v = 10 OR a.v = 20) AND NOT a.id = 2",
}


class TestSweepSQLiteAgreement(WarehouseTestCase):
    def setUp(self):
        super().setUp()
        self.make_warehouse(SCHEMA)

    def test_each_shape_agrees_with_sqlite_multiset(self):
        for name, sql in QUERIES.items():
            with self.subTest(name):
                result = self.build(sql, name.replace(" ", "_") + ".sqlite")
                reader = self.reader(result)
                self.assert_multisets_equal(
                    self.result_multiset(reader), self.direct_multiset(sql), name
                )

    def test_each_exclusion_agrees_with_physical_deletion(self):
        exclusions = {
            "self join": (["a(id=1)"], {"a": [(1,)]}),
            "union positional alignment": (["a(id=1)", "b(id=2)"],
                                           {"a": [(1,)], "b": [(2,)]}),
            "composite primary key": (
                ["c(id='p', n=1)"], {"c": [("p", 1)]},
            ),
            "null join": (["nulls(id=2)"], {"nulls": [(2,)]}),
            "constant projection duplicate collapsing": (
                ["a(id=1)"], {"a": [(1,)]},
            ),
        }
        for name, (identities, deletions) in exclusions.items():
            sql = QUERIES[name]
            with self.subTest(name):
                result = self.build(sql, name.replace(" ", "_") + ".ex.sqlite")
                engine = self.engine_exclusion_multiset(result, identities)
                oracle = self.deletion_multiset(sql, deletions)
                self.assert_multisets_equal(engine, oracle, name)

    def test_null_and_empty_handling(self):
        # NULL comparisons never join; three-valued WHERE keeps only TRUE.
        result = self.build(
            "SELECT n.id AS id FROM nulls AS n WHERE n.x = 'x'", "nullfilter.sqlite"
        )
        reader = self.reader(result)
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(
                "SELECT n.id AS id FROM nulls AS n WHERE n.x = 'x'"
            ),
        )
        self.assertEqual(len(reader.rows()), 2)


class TestNameFolding(WarehouseTestCase):
    def test_table_and_cte_names_are_ascii_case_insensitive(self):
        self.make_warehouse("""
        CREATE TABLE MixedCase(id INTEGER PRIMARY KEY, v);
        INSERT INTO MixedCase VALUES (1, 'a'), (2, 'b');
        """)
        result = self.build("SELECT v AS v FROM MIXEDCASE")
        reader = self.reader(result)
        self.assertEqual(
            {tuple(x.key() for x in row.values) for row in reader.rows()},
            {("t:a",), ("t:b",)},
        )
        # Token identity uses the stored (canonical) table spelling.
        self.assertEqual(
            {b.table for b in reader.base_identities()}, {"MixedCase"}
        )
        outcome = evaluate_exclusion(result, ["mixedcase(id=1)"])
        self.assertEqual(outcome["total_after"], 1)


class TestColourCollationSweep(WarehouseTestCase):
    def test_column_collations_across_operations(self):
        self.make_warehouse("""
        CREATE TABLE nc(id INTEGER PRIMARY KEY, t TEXT COLLATE NOCASE);
        CREATE TABLE bin(id INTEGER PRIMARY KEY, t TEXT COLLATE BINARY);
        INSERT INTO nc VALUES (1, 'AbC'), (2, 'aBc'), (3, 'xyz');
        INSERT INTO bin VALUES (1, 'AbC');
        """)
        cases = {
            "nocase filter": "SELECT nc.t AS t FROM nc WHERE nc.t = 'aBc'",
            "binary filter": "SELECT bin.t AS t FROM bin WHERE bin.t = 'abc'",
            "nocase join left": (
                "SELECT nc.t AS t FROM nc JOIN bin ON nc.t = bin.t"
            ),
            "nocase join reversed": (
                "SELECT bin.t AS t FROM bin JOIN nc ON bin.t = nc.t"
            ),
            "explicit collate binary": (
                "SELECT nc.t AS t FROM nc WHERE nc.t COLLATE BINARY = 'abc'"
            ),
            "nocase in": "SELECT nc.t AS t FROM nc WHERE nc.t IN ('aBc')",
            "nocase like": "SELECT nc.t AS t FROM nc WHERE nc.t LIKE 'ab%'",
        }
        for name, sql in cases.items():
            with self.subTest(name):
                result = self.build(sql, "coll_" + name.replace(" ", "_") + ".sqlite")
                reader = self.reader(result)
                self.assert_multisets_equal(
                    self.result_multiset(reader), self.direct_multiset(sql), name
                )


class TestSourceTableShapes(WarehouseTestCase):
    def test_composite_pk_order_and_text_keys(self):
        self.make_warehouse("""
        CREATE TABLE comp(region TEXT, code TEXT, v, PRIMARY KEY(code, region));
        INSERT INTO comp VALUES ('n', 'c1', 1), ('s', 'c1', 2), ('e', 'c2', 3);
        """)
        result = self.build("SELECT v AS v FROM comp")
        reader = self.reader(result)
        identities = reader.base_identities()
        # Declared primary-key order is (code, region); token values follow it.
        self.assertEqual(identities[0].pk_columns, ("code", "region"))
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset("SELECT v AS v FROM comp")
        )

    def test_text_primary_key_and_blob_identities(self):
        self.make_warehouse("""
        CREATE TABLE t(k TEXT PRIMARY KEY, v);
        INSERT INTO t VALUES ('a b', 1), ('', 2), ('x', 3);
        """)
        result = self.build("SELECT v AS v FROM t")
        reader = self.reader(result)
        outcome = evaluate_exclusion(result, ["t(k='a b')"])
        self.assertEqual(outcome["total_after"], 2)
        outcome = evaluate_exclusion(result, ["t(k='')"])
        self.assertEqual(outcome["total_after"], 2)
        outcome = evaluate_exclusion(result, ["t(k='a b')", "t(k='')"])
        self.assertEqual(outcome["total_after"], 1)

    def test_generated_columns_remain_visible(self):
        self.make_warehouse("""
        CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT, g TEXT GENERATED ALWAYS AS (v || '!') VIRTUAL);
        INSERT INTO t VALUES (1, 'x');
        """)
        result = self.build("SELECT * FROM t")
        reader = self.reader(result)
        self.assertEqual(reader.column_names(), ["id", "v", "g"])
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset("SELECT * FROM t")
        )


def evaluate_exclusion_for_test(*args, **kwargs):
    from provenance_query import evaluate_exclusion

    return evaluate_exclusion(*args, **kwargs)


# Local import shim so the test module reads naturally.
from provenance_query import evaluate_exclusion  # noqa: E402


if __name__ == "__main__":
    unittest.main()
