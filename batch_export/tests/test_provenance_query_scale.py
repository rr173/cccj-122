"""Structural/compactness tests including the astronomical 2000^6 case.

The acceptance for the large case is the *compact representation and shared
token evaluation*, not "the small sample runs".  The test:

* builds 2000 base rows through a real warehouse;
* a CTE projects them to one constant tuple;
* six CROSS JOINs of that CTE;
* checks one result tuple whose original multiplicity is 2000^6;
* excludes one base token and checks (2000-1)^6 exactly, without running or
  expanding the ordinary query;
* records graph node/edge counts, peak RSS and elapsed time, then doubles the
  input rows and checks graph growth.
"""
from __future__ import annotations

import os
import resource
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pq_helpers import WarehouseTestCase

from provenance_query import ProvenanceResultReader, evaluate_exclusion  # noqa: E402

BASE_ROWS = 2000


def big_query() -> str:
    return (
        "WITH c AS (SELECT 1 AS one FROM facts) "
        "SELECT c.one AS one FROM c "
        "CROSS JOIN c AS c2 CROSS JOIN c AS c3 CROSS JOIN c AS c4 "
        "CROSS JOIN c AS c5 CROSS JOIN c AS c6"
    )


class TestAstronomicalCompactness(WarehouseTestCase):
    def setUp(self):
        super().setUp()
        self.make_facts(BASE_ROWS)

    def make_facts(self, n: int):
        conn = sqlite3_connect(self.db_path)
        try:
            conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, v)")
            conn.executemany(
                "INSERT INTO facts VALUES (?, ?)",
                [(i, f"v{i}") for i in range(1, n + 1)],
            )
            conn.commit()
        finally:
            conn.close()

    def append_facts(self, start: int, stop: int):
        conn = sqlite3_connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO facts VALUES (?, ?)",
                [(i, f"v{i}") for i in range(start, stop + 1)],
            )
            conn.commit()
        finally:
            conn.close()

    def test_2000_to_the_sixth_is_compact_and_exact(self):
        sql = big_query()
        rss_before = _peak_rss_mb()
        started = time.time()
        result = self.build(sql, "big.sqlite")
        elapsed = time.time() - started
        peak_rss = max(rss_before, _peak_rss_mb())
        reader = self.reader(result)
        rows = reader.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].multiplicity, 2000 ** 6)
        meta = reader.meta()
        nodes = int(meta["graph_nodes"])
        edges = int(meta["graph_edges"])
        tokens = int(meta["tokens"])
        self.assertEqual(tokens, 2000)
        # Linear growth in table size, not exponential.
        self.assertLess(nodes, 20_000)
        self.assertLess(edges, 40_000)
        self.assertLess(elapsed, 30.0)
        self.assertLess(peak_rss, 400.0)
        print(
            f"  big case: rows=1 multiplicity=2000^6 nodes={nodes} edges={edges}"
            f" tokens={tokens} elapsed={elapsed:.3f}s peak_rss={peak_rss:.1f}MB"
        )
        # Excluding one base token gives 1999^6 exactly.
        outcome = evaluate_exclusion(result, ["facts(id=1)"], reader=reader)
        self.assertEqual(outcome["total_after"], 1999 ** 6)
        self.assertLess(nodes * 2, edges + 40_000)

    def test_doubling_input_rows_doubles_graph_scale(self):
        sql = big_query()
        result_small = self.build(sql, "small.sqlite")
        small = ProvenanceResultReader(result_small)
        small_nodes = int(small.meta()["graph_nodes"])
        small_tokens = int(small.meta()["tokens"])
        small.close()
        self.append_facts(BASE_ROWS + 1, 2 * BASE_ROWS)
        result_large = self.build(sql, "large.sqlite")
        large = ProvenanceResultReader(result_large)
        large_nodes = int(large.meta()["graph_nodes"])
        large_tokens = int(large.meta()["tokens"])
        large_rows = large.rows()
        large.close()
        self.assertEqual(large_tokens, 2 * BASE_ROWS)
        self.assertEqual(large_rows[0].multiplicity, (2 * BASE_ROWS) ** 6)
        # Node growth is linear in token count (~2x), never exponential.
        self.assertLess(large_nodes, 2 * small_nodes + 100)
        self.assertLess(large_tokens, 2 * small_tokens + 10)
        print(
            f"  growth: tokens {small_tokens} -> {large_tokens};"
            f" nodes {small_nodes} -> {large_nodes}"
        )


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def sqlite3_connect(path):
    import sqlite3

    return sqlite3.connect(path)


if __name__ == "__main__":
    unittest.main()
