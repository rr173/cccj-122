"""End-to-end demo + independent-oracle agreement tests.

The demo script itself is the teaching artifact; here we (a) run it under
``-I`` in a subprocess to prove it is self-contained, and (b) cross-check the
same XBRL warehouse with the provenance engine and with plain SQLite.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from provenance_query import (  # noqa: E402
    ProvenanceQueryBuilder,
    ProvenanceResultReader,
    TyValue,
    evaluate_exclusion,
)
from xbrl_ingest import XbrlIngestor  # noqa: E402

DEMO_DIR = os.path.join(PROJECT, "demo")
FIXTURES = os.path.join(DEMO_DIR, "xbrl_fixtures")
DICTIONARY = os.path.join(FIXTURES, "concepts.json")


def run_demo_subprocess() -> subprocess.CompletedProcess:
    launcher = (
        "import sys, runpy, site\n"
        f"sys.path.insert(0, {PROJECT!r})\n"
        "site.addsitedir(site.getusersitepackages())\n"
        f"runpy.run_path({os.path.join(DEMO_DIR, 'run_provenance_query_demo.py')!r},"
        " run_name='__main__')\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", launcher],
        capture_output=True, text=True, timeout=300,
    )


class TestDemoScript(unittest.TestCase):
    def test_demo_runs_under_isolated_mode(self):
        outcome = run_demo_subprocess()
        self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
        self.assertIn("all 33 checks held", outcome.stdout)
        self.assertIn("(a1+a2)*(b1+b2)", outcome.stdout)
        self.assertIn("graph leaves point at stored rows", outcome.stdout)


class TestXbrlWarehouseOracle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pq_xbrl_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "facts.sqlite")
        ingestor = XbrlIngestor(self.db, DICTIONARY)
        for name in ("demo-A.xml", "demo-B.xml", "demo-C.xml", "demo-D.xml"):
            ingestor.ingest_file(os.path.join(FIXTURES, name))
        ingestor.close()

    def build(self, sql: str, name: str) -> str:
        sql_path = os.path.join(self.tmp, name + ".sql")
        with open(sql_path, "w", encoding="utf-8") as handle:
            handle.write(sql)
        result = os.path.join(self.tmp, "result_" + name + ".sqlite")
        if os.path.exists(result):
            os.remove(result)
        ProvenanceQueryBuilder(self.db).build(sql_path, result)
        return result

    def oracle(self, sql: str) -> dict:
        conn = sqlite3.connect(self.db)
        conn.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
        try:
            out = {}
            for row in conn.execute(sql).fetchall():
                key = tuple(TyValue.from_sqlite(v).key() for v in row)
                out[key] = out.get(key, 0) + 1
            return out
        finally:
            conn.close()

    def engine(self, reader) -> dict:
        out = {}
        for row in reader.rows():
            key = tuple(v.key() for v in row.values)
            out[key] = out.get(key, 0) + row.multiplicity
        return out

    def test_join_over_facts_sources_contexts_agrees(self):
        sql = (
            "SELECT f.concept AS concept, c.entity_identifier AS entity,"
            " s.source_id AS source"
            " FROM facts AS f"
            " JOIN sources AS s ON s.source_id = f.source_id"
            " JOIN contexts AS c ON c.context_digest = f.context_digest"
            " WHERE f.concept_type = 'decimal'"
        )
        result = self.build(sql, "facts")
        reader = ProvenanceResultReader(result)
        try:
            self.assertEqual(self.engine(reader), self.oracle(sql))
            # Leaves must resolve to actual stored rows.
            conn = sqlite3.connect(self.db)
            try:
                seen_tables = set()
                for identity in reader.base_identities():
                    seen_tables.add(identity.table)
                    where = " AND ".join(
                        f'"{c}" = ?' for c in identity.pk_columns
                    )
                    count = conn.execute(
                        f'SELECT COUNT(*) FROM "{identity.table}" WHERE {where}',
                        tuple(v.value for v in identity.pk_values),
                    ).fetchone()[0]
                    self.assertEqual(count, 1, identity.describe())
                self.assertEqual(seen_tables, {"facts", "sources", "contexts"})
            finally:
                conn.close()
        finally:
            reader.close()

    def test_excluding_one_fact_matches_physical_deletion(self):
        sql = (
            "SELECT f.concept AS concept, c.entity_identifier AS entity"
            " FROM facts AS f JOIN contexts AS c"
            " ON c.context_digest = f.context_digest"
        )
        result = self.build(sql, "facts2")
        conn = sqlite3.connect(self.db)
        first_id = conn.execute(
            "SELECT id FROM facts ORDER BY id LIMIT 1"
        ).fetchone()[0]
        oracle_copy = os.path.join(self.tmp, "deleted.sqlite")
        shutil.copyfile(self.db, oracle_copy)
        conn.close()
        deleted = sqlite3.connect(oracle_copy)
        try:
            deleted.execute("DELETE FROM facts WHERE id = ?", (first_id,))
            deleted.commit()
            expected = {}
            for row in deleted.execute(sql).fetchall():
                key = tuple(TyValue.from_sqlite(v).key() for v in row)
                expected[key] = expected.get(key, 0) + 1
        finally:
            deleted.close()
        outcome = evaluate_exclusion(result, [f"facts(id={first_id})"])
        actual = {}
        for row in outcome["rows"]:
            if row["after"]:
                key = tuple(TyValue.decode(v).key() for v in row["values"])
                actual[key] = actual.get(key, 0) + row["after"]
        self.assertEqual(actual, expected)

    def test_whole_source_excluded_matches_deletion(self):
        conn = sqlite3.connect(self.db)
        source_id = conn.execute(
            "SELECT source_id FROM sources ORDER BY ingest_seq LIMIT 1"
        ).fetchone()[0]
        conn.close()
        sql = (
            "SELECT f.id AS id, s.original_name AS name"
            " FROM facts AS f JOIN sources AS s"
            " ON s.source_id = f.source_id"
        )
        result = self.build(sql, "src")
        oracle_copy = os.path.join(self.tmp, "srcdeleted.sqlite")
        shutil.copyfile(self.db, oracle_copy)
        deleted = sqlite3.connect(oracle_copy)
        try:
            deleted.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
            deleted.commit()
            expected = {}
            for row in deleted.execute(sql).fetchall():
                key = tuple(TyValue.from_sqlite(v).key() for v in row)
                expected[key] = expected.get(key, 0) + 1
        finally:
            deleted.close()
        outcome = evaluate_exclusion(
            result, [f"sources(source_id='{source_id}')"]
        )
        actual = {}
        for row in outcome["rows"]:
            if row["after"]:
                key = tuple(TyValue.decode(v).key() for v in row["values"])
                actual[key] = actual.get(key, 0) + row["after"]
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
