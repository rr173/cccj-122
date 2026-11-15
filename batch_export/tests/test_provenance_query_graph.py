"""Shared-DAG tests: structural interning, deep/diamond evaluation, scaling.

These test the graph module directly; they do not go through SQL.
"""
from __future__ import annotations

import os
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from provenance_query.graph import ProvenanceGraph  # noqa: E402


def make_leaf(graph):
    token = graph.universe.token("t", "t", ("id",), [])
    return token, graph.token(token)


class TestSharedDag(unittest.TestCase):
    def test_structural_interning_shares_nodes(self):
        g = ProvenanceGraph()
        t1, a1 = make_leaf(g)
        t2 = g.universe.token("t", "t", ("id",), [])
        # A second call with no token may not be useful; create another row.
        g2 = ProvenanceGraph()
        _ta, a = make_leaf(g2)
        _tb, b = make_leaf(g2)
        ab = g2.add(a, b)
        ba = g2.add(b, a)
        self.assertEqual(ab, ba, "add is commutative and interned")
        product = g2.mul(a, b)
        product2 = g2.mul(b, a)
        self.assertEqual(product, product2)
        # The same (a+b)*(a+b) does not duplicate the (a+b) subtree.
        before = g2.node_count
        _ = g2.mul(ab, ab)
        self.assertEqual(g2.node_count, before + 1)

    def test_token_used_twice_is_one_node(self):
        g = ProvenanceGraph()
        token, leaf = make_leaf(g)
        again = g.token(token)
        self.assertEqual(leaf, again)

    def test_deep_chain_evaluation_is_iterative(self):
        g = ProvenanceGraph()
        token, leaf = make_leaf(g)
        node = leaf
        for _ in range(20000):
            node = g.add(node, leaf)
        self.assertEqual(g.evaluate(node), 20001)
        self.assertEqual(g.evaluate(node, frozenset({token})), 0)

    def test_diamond_evaluation_memoised(self):
        g = ProvenanceGraph()
        token, leaf = make_leaf(g)
        node = leaf
        for _ in range(40):
            node = g.add(node, node)
        # 2^40 copies of the token, computed in O(nodes) despite the diamond
        # shape; token present -> value 2^40.
        self.assertEqual(g.evaluate(node), 2 ** 40)
        self.assertEqual(g.evaluate(node, frozenset({token})), 0)

    def test_multiplication_repeats_token(self):
        g = ProvenanceGraph()
        token, leaf = make_leaf(g)
        node = g.mul(g.mul(leaf, leaf), g.mul(leaf, leaf))
        self.assertEqual(g.evaluate(node), 1)
        self.assertEqual(g.evaluate(node, frozenset({token})), 0)

    def test_arbitrary_precision(self):
        g = ProvenanceGraph()
        token, leaf = make_leaf(g)
        node = leaf
        for _ in range(200):
            node = g.add(node, node)
        self.assertEqual(g.evaluate(node), 2 ** 200)
        value = g.evaluate(node)
        self.assertGreater(value, 2 ** 64)

    def test_encode_decode_round_trip(self):
        g = ProvenanceGraph()
        token, leaf = make_leaf(g)
        root = g.add(g.mul(leaf, leaf), g.one)
        payload = g.encode()
        g2 = ProvenanceGraph()
        g2.universe.token("t", "t", ("id",), [])
        restored = ProvenanceGraph.decode(payload, g2.universe)
        self.assertEqual(restored.evaluate(root), g.evaluate(root))
        self.assertEqual(restored.node_count, g.node_count)


class TestCompactStructures(unittest.TestCase):
    def test_branching_union_annotation_stays_linear(self):
        g = ProvenanceGraph()
        token, leaf = make_leaf(g)
        # 1000 alternative derivations of one tuple: a left-folded add chain
        # costs one node per derivation (linear); it never expands into
        # alternative *sets*.
        node = leaf
        for _ in range(999):
            node = g.add(node, leaf)
        self.assertLessEqual(g.node_count, 1005)
        self.assertGreaterEqual(g.node_count, 3)
        self.assertEqual(g.evaluate(node), 1000)
        self.assertEqual(g.evaluate(node, frozenset({token})), 0)

    def test_shared_subexpression_is_not_copied_per_product(self):
        g = ProvenanceGraph()
        _ta, a = make_leaf(g)
        _tb, b = make_leaf(g)
        _tc, c = make_leaf(g)
        shared = g.add(a, b)
        root = g.mul(g.mul(shared, c), shared)
        # One shared node is referenced twice; it is not duplicated.
        self.assertEqual(g.evaluate(root), 4)
        references = sum(
            1 for kind, args in zip(g.kinds, g.args)
            if kind in ("add", "mul") and shared in args
        )
        self.assertEqual(references, 2)


if __name__ == "__main__":
    unittest.main()
