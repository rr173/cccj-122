"""Behavioural tests for provenance-carrying queries.

Independent expectations: the same SQL runs in raw SQLite and rows are turned
into a typed multiset with Python; exclusions compare against a second copy
of the warehouse where the rows are actually deleted.
"""
from __future__ import annotations

import os
import sqlite3
import unittest

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pq_helpers import WarehouseTestCase, result_multiset

BASIC = """
CREATE TABLE left_t(id INTEGER PRIMARY KEY, v TEXT);
CREATE TABLE right_t(id INTEGER PRIMARY KEY, v TEXT);
INSERT INTO left_t VALUES (1,'a1'), (2,'a2');
INSERT INTO right_t VALUES (1,'b1'), (2,'b2');
"""


class TestMultiplicityBasics(WarehouseTestCase):
    def test_demo_cross_join_projects_same_value(self):
        self.make_warehouse(BASIC)
        sql = "SELECT 'same' AS v FROM left_t CROSS JOIN right_t"
        result = self.build(sql)
        reader = self.reader(result)
        rows = reader.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].multiplicity, 4)
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(sql)
        )
        # exclude a1 -> 2
        outcome = self.engine_exclusion_multiset(result, ["left_t(id=1)"])
        self.assert_multisets_equal(
            outcome, self.deletion_multiset(sql, {"left_t": [(1,)]})
        )
        self.assertEqual(sum(outcome.values()), 2)
        # exclude a1 and b1 -> 1
        outcome = self.engine_exclusion_multiset(
            result, ["left_t(id=1)", "right_t(id=1)"]
        )
        self.assert_multisets_equal(
            outcome,
            self.deletion_multiset(
                sql, {"left_t": [(1,)],
                      "right_t": [(1,)]}
            ),
        )
        self.assertEqual(sum(outcome.values()), 1)

    def test_union_all_twice_doubles(self):
        self.make_warehouse(BASIC)
        sql = (
            "SELECT 'same' AS v FROM left_t CROSS JOIN right_t"
            " UNION ALL SELECT 'same' AS v FROM left_t CROSS JOIN right_t"
        )
        result = self.build(sql)
        reader = self.reader(result)
        self.assertEqual(reader.rows()[0].multiplicity, 8)
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(sql)
        )
        exclude_a1 = self.engine_exclusion_multiset(result, ["left_t(id=1)"])
        self.assertEqual(sum(exclude_a1.values()), 4)
        exclude_both = self.engine_exclusion_multiset(
            result, ["left_t(id=1)", "right_t(id=1)"]
        )
        self.assertEqual(sum(exclude_both.values()), 2)

    def test_self_cross_join_squares_the_sum(self):
        self.make_warehouse(BASIC)
        sql = "SELECT 'same' AS v FROM left_t CROSS JOIN left_t AS l2"
        result = self.build(sql)
        reader = self.reader(result)
        self.assertEqual(reader.rows()[0].multiplicity, 4)
        # Excluding a1 must leave 1 (the (a2,a2) derivation), not 2: the token
        # is an atom multiplied twice, never a set that de-duplicates.
        outcome = self.engine_exclusion_multiset(result, ["left_t(id=1)"])
        self.assertEqual(sum(outcome.values()), 1)
        oracle = self.deletion_multiset(sql, {"left_t": [(1,)]})
        self.assert_multisets_equal(outcome, oracle)

    def test_distinct_values_do_not_merge(self):
        self.make_warehouse(BASIC)
        sql = "SELECT left_t.v AS x, right_t.v AS y FROM left_t CROSS JOIN right_t"
        result = self.build(sql)
        reader = self.reader(result)
        self.assertEqual(len(reader.rows()), 4)
        self.assert_multisets_equal(
            self.result_multiset(reader), self.direct_multiset(sql)
        )
        outcome = self.engine_exclusion_multiset(result, ["left_t(id=1)"])
        self.assert_multisets_equal(
            outcome, self.deletion_multiset(sql, {"left_t": [(1,)]})
        )


class TestIntermediateDuplicates(WarehouseTestCase):
    """Duplicate collapsing happens before later relational work."""

    def test_constant_projection_then_union(self):
        self.make_warehouse("""
        CREATE TABLE t(id INTEGER PRIMARY KEY, v);
        INSERT INTO t VALUES (1, 'a'), (2, 'b');
        """)
        sql = (
            "WITH c AS (SELECT 1 AS one FROM t) "
            "SELECT one FROM c UNION ALL SELECT one FROM c"
        )
        result = self.build(sql)
        reader = self.reader(result)
        self.assertEqual(len(reader.rows()), 1)
        self.assertEqual(reader.rows()[0].multiplicity, 4)
        outcome = self.engine_exclusion_multiset(result, ["t(id=1)"])
        self.assertEqual(sum(outcome.values()), 2)

    def test_constant_projection_then_self_join(self):
        self.make_warehouse("""
        CREATE TABLE t(id INTEGER PRIMARY KEY, v);
        INSERT INTO t VALUES (1, 'a'), (2, 'b');
        """)
        sql = (
            "WITH c AS (SELECT 1 AS one FROM t) "
            "SELECT x.one AS one FROM c AS x CROSS JOIN c AS y"
        )
        result = self.build(sql)
        reader = self.reader(result)
        self.assertEqual(reader.rows()[0].multiplicity, 4)
        self.assertEqual(
            sum(self.engine_exclusion_multiset(result, ["t(id=1)"]).values()), 1
        )


class TestHandWrittenDemoAlgebra(WarehouseTestCase):
    """The algebra taught to colleagues: (a1+a2)*(b1+b2)."""

    def test_expression_structure_and_evaluation(self):
        self.make_warehouse(BASIC)
        sql = "SELECT 'x' AS v FROM left_t CROSS JOIN right_t"
        result = self.build(sql)
        reader = self.reader(result)
        graph = reader.graph()
        row = reader.rows()[0]
        # The root's evaluation is the original multiplicity.
        self.assertEqual(graph.evaluate(row.graph_root), 4)
        # Excluding exactly a1 yields 2; a1 and b1 yields 1.
        identity = {b.table: b for b in reader.base_identities()}
        a1 = [b for b in reader.base_identities() if b.table == "left_t"
              and b.pk_values[0].display() == "1"][0]
        a2 = [b for b in reader.base_identities() if b.table == "left_t"
              and b.pk_values[0].display() == "2"][0]
        b1 = [b for b in reader.base_identities() if b.table == "right_t"
              and b.pk_values[0].display() == "1"][0]
        b2 = [b for b in reader.base_identities() if b.table == "right_t"
              and b.pk_values[0].display() == "2"][0]
        self.assertEqual(graph.evaluate(row.graph_root, frozenset({a1.token})), 2)
        self.assertEqual(
            graph.evaluate(row.graph_root, frozenset({a1.token, b1.token})), 1
        )
        self.assertEqual(
            graph.evaluate(
                row.graph_root,
                frozenset({a1.token, a2.token, b1.token, b2.token}),
            ),
            0,
        )
        # The expression is equivalent to (a1+a2)*(b1+b2): evaluate every
        # subset of tokens and compare against the factored formula.
        tokens = {a1.token: "a1", a2.token: "a2", b1.token: "b1", b2.token: "b2"}
        for mask in range(16):
            excluded = frozenset(
                token for bit, token in enumerate(tokens) if mask & (1 << bit)
            )
            kept = {name: 0 if token in excluded else 1
                    for token, name in tokens.items()}
            expected = (kept["a1"] + kept["a2"]) * (kept["b1"] + kept["b2"])
            self.assertEqual(
                graph.evaluate(row.graph_root, excluded), expected,
                f"subset {kept}",
            )
        # The graph is shared, not a per-derivation copy: four product nodes
        # reference the same four token leaves.
        token_leaves = [n for n in range(graph.node_count) if graph.kind(n) == "token"]
        self.assertEqual(len(token_leaves), 4)


if __name__ == "__main__":
    unittest.main()
