"""Result-file format, offline reading, exclusion evaluation and CLI tests."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pq_helpers import WarehouseTestCase

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from provenance_query import (  # noqa: E402
    BuildError,
    ExclusionError,
    ProvenanceQueryBuilder,
    ProvenanceResultReader,
    evaluate_exclusion,
)
from provenance_query.cli import main as cli_main  # noqa: E402
from provenance_query.errors import (  # noqa: E402
    EXIT_BUILD,
    EXIT_INVOCATION,
    EXIT_UNSUPPORTED,
)

BASIC = """
CREATE TABLE l(id INTEGER PRIMARY KEY, v TEXT);
CREATE TABLE r(id INTEGER PRIMARY KEY, v TEXT);
INSERT INTO l VALUES (1,'a1'),(2,'a2');
INSERT INTO r VALUES (1,'b1'),(2,'b2');
"""


class TestResultFile(WarehouseTestCase):
    def test_result_file_reopens_after_source_removed(self):
        self.make_warehouse(BASIC)
        sql = "SELECT 'same' AS v FROM l CROSS JOIN r"
        result = self.build(sql)
        reader = self.reader(result)
        before = reader.raw_compressed()
        self.assertEqual(before["meta"]["format"], "provenance-query-result")
        self.assertEqual(len(before["rows"]), 1)
        self.assertEqual(before["rows"][0]["multiplicity"], "4")
        # Move the source away entirely.
        os.remove(self.db_path)
        reopened = ProvenanceResultReader(result)
        try:
            again = reopened.raw_compressed()
            self.assertEqual(again, before)
            outcome = evaluate_exclusion(result, ["l(id=1)"], reader=reopened)
            self.assertEqual(outcome["total_after"], 2)
            self.assertEqual(reopened.validate(), [])
        finally:
            reopened.close()

    def test_refuses_to_overwrite_existing_target(self):
        self.make_warehouse(BASIC)
        sql_path = self.write_sql("SELECT v AS v FROM l", "q.sql")
        result = os.path.join(self.result_dir, "existing.sqlite")
        # First build succeeds.
        from provenance_query import ProvenanceQueryBuilder

        ProvenanceQueryBuilder(self.db_path).build(sql_path, result)
        with self.assertRaises(BuildError) as ctx:
            ProvenanceQueryBuilder(self.db_path).build(sql_path, result)
        self.assertIn("already exists", str(ctx.exception))

    def test_failed_build_leaves_no_partial_file(self):
        self.make_warehouse(BASIC)
        sql_path = self.write_sql("SELECT DISTINCT v FROM l", "bad.sql")
        result = os.path.join(self.result_dir, "never.sqlite")
        from provenance_query import ProvenanceQueryBuilder
        from provenance_query.errors import UnsupportedQueryError

        with self.assertRaises(UnsupportedQueryError):
            ProvenanceQueryBuilder(self.db_path).build(sql_path, result)
        self.assertFalse(os.path.exists(result))
        # No temporary files left behind.
        leftovers = [
            name for name in os.listdir(self.result_dir)
            if name.startswith(".provenance-result-")
        ]
        self.assertEqual(leftovers, [])

    def test_graph_leaf_points_at_real_base_row(self):
        self.make_warehouse(BASIC)
        sql = "SELECT 'same' AS v FROM l CROSS JOIN r"
        result = self.build(sql)
        reader = self.reader(result)
        identities = reader.base_identities()
        self.assertEqual(len(identities), 4)
        # Verify each identity really is a row in the warehouse.
        conn = sqlite3.connect(self.copy_warehouse("check.sqlite"))
        try:
            for identity in identities:
                select = "SELECT COUNT(*) FROM " + '"' + identity.table + '" WHERE '
                select += " AND ".join(
                    f'"{c}" = ?' for c in identity.pk_columns
                )
                count = conn.execute(
                    select,
                    tuple(v.value for v in identity.pk_values),
                ).fetchone()[0]
                self.assertEqual(count, 1, identity.describe())
        finally:
            conn.close()

    def test_multiplicities_remain_arbitrary_precision_through_json(self):
        self.make_warehouse("""
        CREATE TABLE t(id INTEGER PRIMARY KEY, v);
        INSERT INTO t VALUES (1, 'a'), (2, 'b');
        """)
        sql = "SELECT 'x' AS v FROM t CROSS JOIN t AS t2 CROSS JOIN t AS t3"
        result = self.build(sql)
        reader = self.reader(result)
        payload = reader.raw_compressed()
        self.assertEqual(payload["rows"][0]["multiplicity"], "8")
        # A JSON round trip keeps the decimal string exactly.
        text = json.dumps(payload)
        self.assertIn('"multiplicity": "8"', text)


class TestExclusionEvaluation(WarehouseTestCase):
    def setUp(self):
        super().setUp()
        self.make_warehouse(BASIC)

    def test_unknown_identity_is_a_caller_error(self):
        result = self.build("SELECT v AS v FROM l")
        with self.assertRaises(ExclusionError) as ctx:
            evaluate_exclusion(result, ["l(id=99)"])
        self.assertIn("not present in this result", str(ctx.exception))
        self.assertIn("input snapshot", str(ctx.exception))

    def test_duplicate_identities_exclude_once(self):
        result = self.build("SELECT v AS v FROM l")
        outcome = evaluate_exclusion(result, ["l(id=1)", "l(id=1)"])
        self.assertEqual(outcome["excluded_count"], 1)
        self.assertEqual(outcome["total_before"], 2)
        self.assertEqual(outcome["total_after"], 1)

    def test_malformed_identity_is_rejected(self):
        result = self.build("SELECT v AS v FROM l")
        for identity in ["l(id=x)", "l(id", "l()", "l(id=1)extra"]:
            with self.assertRaises(ExclusionError):
                evaluate_exclusion(result, [identity])

    def test_typed_identity_distinguishes_storage_classes(self):
        self.make_warehouse("""
        CREATE TABLE t(id INTEGER PRIMARY KEY, v);
        INSERT INTO t VALUES (1, 'x'), (2, 'y');
        CREATE TABLE s(k TEXT PRIMARY KEY, w);
        INSERT INTO s VALUES ('5', 'p'), ('05', 'q');
        """)
        result = self.build("SELECT w AS w FROM s")
        # Both text keys exist; excluding '5' must not exclude '05'.
        outcome = evaluate_exclusion(result, ["s(k='5')"])
        self.assertEqual(outcome["total_after"], 1)
        with self.assertRaises(ExclusionError):
            evaluate_exclusion(result, ["s(k=5)"])  # integer, not text

    def test_exclusion_does_not_modify_result_file(self):
        result = self.build("SELECT 'same' AS v FROM l CROSS JOIN r")
        before = os.stat(result).st_mtime_ns, os.path.getsize(result)
        evaluate_exclusion(result, ["l(id=1)"])
        after = os.stat(result).st_mtime_ns, os.path.getsize(result)
        self.assertEqual(before, after)

    def test_row_becomes_zero_but_stays_in_output(self):
        result = self.build("SELECT v AS v FROM l")
        outcome = evaluate_exclusion(result, ["l(id=1)"])
        rows = {row["ordinal"]: row for row in outcome["rows"]}
        self.assertEqual(rows[0]["after"], 0)
        self.assertEqual(rows[1]["after"], 1)


class TestSpecialPaths(WarehouseTestCase):
    def test_space_and_hash_in_paths_survive_read_only_uri(self):
        directory = tempfile.mkdtemp(prefix="pq sp ace?# ", dir=self.tmp)
        db = os.path.join(directory, "ware house.sqlite")
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE t(id INTEGER PRIMARY KEY, v);"
            " INSERT INTO t VALUES (1, 'a');"
        )
        conn.commit()
        conn.close()
        sql_path = os.path.join(directory, "query file.sql")
        with open(sql_path, "w", encoding="utf-8") as handle:
            handle.write("SELECT v AS v FROM t")
        result = os.path.join(directory, "result file.sqlite")
        report = ProvenanceQueryBuilder(db).build(sql_path, result)
        self.assertEqual(report.result_rows, 1)
        reader = self.reader(result)
        self.assertEqual(
            {tuple(v.key() for v in row.values): row.multiplicity
             for row in reader.rows()},
            {("t:a",): 1},
        )


class TestReadOnlyAndSnapshot(WarehouseTestCase):
    def test_source_db_is_untouched_and_no_sidecar_files(self):
        self.make_warehouse(BASIC)
        import hashlib
        import glob
        before = {
            os.path.basename(p): hashlib.sha256(open(p, "rb").read()).hexdigest()
            for p in glob.glob(self.db_path + "*")
        }
        result = self.build("SELECT v AS v FROM l")
        after = {
            os.path.basename(p): hashlib.sha256(open(p, "rb").read()).hexdigest()
            for p in glob.glob(self.db_path + "*")
        }
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(self.db_path + "-wal"))
        self.assertFalse(os.path.exists(self.db_path + "-shm"))
        self.assertFalse(os.path.exists(self.db_path + "-journal"))

    def test_source_may_change_concurrently_without_mixing(self):
        self.make_warehouse("CREATE TABLE t(id INTEGER PRIMARY KEY, v);"
                            " INSERT INTO t VALUES (1, 'a');")
        import threading
        import time
        sql_path = self.write_sql("SELECT v AS v FROM t")
        result = os.path.join(self.result_dir, "snapshot.sqlite")
        errors = []

        def writer():
            conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
            for i in range(2, 200):
                try:
                    conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"x{i}"))
                    time.sleep(0.001)
                except Exception as exc:  # pragma: no cover - defensive
                    errors.append(str(exc))
            conn.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            from provenance_query import ProvenanceQueryBuilder

            report = ProvenanceQueryBuilder(self.db_path).build(sql_path, result)
        finally:
            thread.join()
        self.assertEqual(errors, [])
        # The snapshot was taken at BEGIN: exactly the rows present then, and
        # the result is internally consistent (a snapshot, not a mixture).
        reader = self.reader(result)
        stored = reader.rows()
        self.assertEqual(len(stored), report.result_rows)
        self.assertEqual(sum(r.multiplicity for r in stored),
                         report.original_multiplicity_total)


class TestCliExitCodes(WarehouseTestCase):
    def run_cli(self, *args):
        code = cli_main(list(args))
        return code

    def test_build_and_read_and_eval(self):
        self.make_warehouse(BASIC)
        sql_path = self.write_sql("SELECT 'same' AS v FROM l CROSS JOIN r")
        result = os.path.join(self.result_dir, "cli.sqlite")
        self.assertEqual(self.run_cli("build", self.db_path, sql_path, result), 0)
        self.assertEqual(self.run_cli("verify", result), 0)
        self.assertEqual(
            self.run_cli("eval", result, "--exclude", "l(id=1)"), 0
        )
        self.assertEqual(
            self.run_cli("read", result, "--json"), 0
        )

    def test_unsupported_sql_exit_code(self):
        self.make_warehouse(BASIC)
        sql_path = self.write_sql("SELECT DISTINCT v FROM l")
        result = os.path.join(self.result_dir, "x.sqlite")
        self.assertEqual(
            self.run_cli("build", self.db_path, sql_path, result),
            EXIT_UNSUPPORTED,
        )

    def test_bad_identity_exit_code(self):
        self.make_warehouse(BASIC)
        sql_path = self.write_sql("SELECT v AS v FROM l")
        result = os.path.join(self.result_dir, "x.sqlite")
        self.assertEqual(self.run_cli("build", self.db_path, sql_path, result), 0)
        self.assertEqual(
            self.run_cli("eval", result, "--exclude", "l(id=9)"),
            EXIT_INVOCATION,
        )

    def test_missing_result_file_exit_code(self):
        self.assertEqual(
            self.run_cli("read", os.path.join(self.result_dir, "nope.sqlite")),
            EXIT_INVOCATION,
        )


if __name__ == "__main__":
    unittest.main()
