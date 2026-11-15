"""End-to-end semantics for controlled_query: effects, masking, typing,
result files, snapshot consistency and the CLI.

Expected rows per identity are written by hand below.  Several checks run
the same SQL through an *independent oracle*: the hand-written authorized
rows are loaded into a small in-memory SQLite database with the same
declared column types, and the query is executed there with plain SQLite --
never through the service's policy resolution or staging code.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cq_helpers as cq
from controlled_query import (
    ControlledQueryService,
    InvocationError,
    QueryExecutionError,
)
from provenance_query.values import TyValue

# Declared types of the published fields, matching the source columns.
RELATION_SHAPES = {
    "fact_lines": [
        ("fact id", "INTEGER"), ("concept", "TEXT"),
        ("raw value", "TEXT"), ("marker", "TEXT"),
    ],
    "shipment_lines": [
        ("ship id", "INTEGER"), ("amount", "INTEGER"), ("note", "TEXT"),
        ("payload", "BLOB"), ("marker", "TEXT"),
    ],
}

# Hand-written authorized rows per identity, per relation.  None = SQL NULL.
EXPECTED = {
    "alice": {
        "fact_lines": [
            (1, "rev", "100", "en"),
            (3, "nil", None, None),
        ],
        "shipment_lines": [
            (1, 100, "ok", b"\x00\xff", "m-old"),
            (5, 9007199254740993, "big", b"", "m-old"),
        ],
    },
    "betty": {
        "fact_lines": [(2, "rev", "200", None)],
        "shipment_lines": [(2, 200, "ok", b"\x01", "m-old")],
    },
    "sally": {
        # summary on E2: "raw value" / amount / payload are NULLed
        "fact_lines": [(2, "rev", None, None)],
        "shipment_lines": [(2, None, "ok", None, "m-old")],
    },
    "ginny": {
        # two groups: full + summary on E1 -> summary
        "fact_lines": [(1, "rev", None, "en"), (3, "nil", None, None)],
        "shipment_lines": [
            (1, None, "ok", None, "m-old"),
            (5, None, "big", None, "m-old"),
        ],
    },
    "danny": {"fact_lines": [], "shipment_lines": []},   # deny beats full
    "nancy": {"fact_lines": [], "shipment_lines": []},   # no grants
    "mixed": {
        "fact_lines": [
            (1, "rev", "100", "en"),
            (3, "nil", None, None),
            (2, "rev", None, None),          # E2 summary row
        ],
        "shipment_lines": [
            (1, 100, "ok", b"\x00\xff", "m-old"),
            (5, 9007199254740993, "big", b"", "m-old"),
            (2, None, "ok", None, "m-old"),  # E2 summary row
        ],
    },
}


def oracle_run(relations, sql):
    """Independent oracle: run ``sql`` with plain SQLite on a small database
    holding the hand-written authorized rows with the same declared types."""
    conn = sqlite3.connect(":memory:")
    try:
        for name, rows in relations.items():
            shape = RELATION_SHAPES[name]
            defs = ", ".join(f'"{col}" {decl}' for col, decl in shape)
            conn.execute(f'CREATE TABLE "{name}" ({defs})')
            placeholders = ", ".join("?" for _ in shape)
            conn.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = [tuple(TyValue.from_sqlite(v) for v in row)
                for row in cursor.fetchall()]
        return columns, rows
    finally:
        conn.close()


def keys(rows):
    return sorted(tuple(v.key() for v in row) for row in rows)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cq_svc_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "wh.sqlite")
        cq.build_warehouse(self.db)
        self.catalog_path = cq.write_catalog(os.path.join(self.tmp, "c.json"))
        self.policy_path = cq.write_policy(os.path.join(self.tmp, "p.json"))
        self.service = ControlledQueryService(
            self.db, self.catalog_path, self.policy_path)

    def query(self, identity, sql):
        return self.service.query(identity, sql=sql)

    def assert_same_rows(self, identity, sql, ordered=False):
        """The service result must equal the independent oracle run of the
        same SQL over the hand-written authorized rows."""
        report = self.query(identity, sql)
        expected_columns, expected_rows = oracle_run(EXPECTED[identity], sql)
        self.assertEqual(report.columns, expected_columns)
        if ordered:
            self.assertEqual([tuple(v.key() for v in r) for r in report.rows],
                             [tuple(v.key() for v in r) for r in expected_rows])
        else:
            self.assertEqual(keys(report.rows), keys(expected_rows))
        return report


class EffectsMatrixTest(ServiceTestCase):
    def test_row_sets_per_identity(self):
        for identity, relations in EXPECTED.items():
            for relation, rows in relations.items():
                report = self.query(identity, f'SELECT * FROM "{relation}"')
                self.assertEqual(
                    keys(report.rows), keys(oracle_run({relation: rows},
                                                       f'SELECT * FROM "{relation}"')[1]),
                    f"{identity}.{relation}",
                )

    def test_same_identifier_different_scheme_stays_separate(self):
        # alice's grant is (lei, ACME-7); entity (national, ACME-7) shares the
        # identifier but is a different entity and stays invisible.
        report = self.query("alice", "SELECT COUNT(*) AS c FROM fact_lines")
        self.assertEqual(cq.display_rows(report), [("2",)])
        report = self.query("betty", "SELECT COUNT(*) AS c FROM fact_lines")
        self.assertEqual(cq.display_rows(report), [("1",)])

    def test_null_and_unmatched_keys_invisible_to_everyone(self):
        # shipments 3 (NULL lane key) and 4 (no such lane) have no
        # confirmable owner; even a hypothetical full grant on every entity
        # could not show them.  Count all visible rows per identity:
        for identity, expected in (("alice", 2), ("betty", 1), ("mixed", 3)):
            report = self.query(identity, "SELECT COUNT(*) AS c FROM shipment_lines")
            self.assertEqual(cq.display_rows(report), [(str(expected),)])

    def test_empty_relations_are_still_queryable(self):
        report = self.query("nancy",
                            "SELECT COUNT(*) AS c, COUNT(concept) AS cc"
                            " FROM fact_lines")
        self.assertEqual(cq.display_rows(report), [("0", "0")])
        report = self.query("danny", "SELECT * FROM shipment_lines")
        self.assertEqual(report.rows, [])

    def test_unknown_identity_is_invocation_error(self):
        with self.assertRaises(InvocationError):
            self.query("mallory", "SELECT 1")


class MaskingSemanticsTest(ServiceTestCase):
    def test_count_star_counts_masked_rows_count_column_does_not(self):
        sql = ('SELECT COUNT(*) AS all_rows, COUNT("raw value") AS valued'
               " FROM fact_lines")
        self.assert_same_rows("sally", sql)   # (1, 0): row visible, value NULL
        self.assert_same_rows("alice", sql)   # (2, 1)

    def test_where_on_hidden_field_matches_nothing(self):
        # The hidden value 200 exists in the source but is NULL for sally;
        # the predicate behaves exactly as if the value had always been NULL.
        sql = 'SELECT "fact id" FROM fact_lines WHERE "raw value" = \'200\''
        self.assert_same_rows("sally", sql)
        self.assert_same_rows("betty", sql)   # full: sees the row

    def test_summary_nulls_flow_through_joins_grouping_and_ordering(self):
        sql = (
            "SELECT s.marker AS m, COUNT(*) AS n, COUNT(s.amount) AS valued,"
            " SUM(s.amount) AS total"
            " FROM shipment_lines s LEFT JOIN fact_lines f"
            "   ON f.marker = s.marker"
            " GROUP BY s.marker"
            " HAVING COUNT(*) >= 1"
            " ORDER BY m"
        )
        self.assert_same_rows("ginny", sql)
        self.assert_same_rows("alice", sql)

    def test_left_join_keeps_left_rows_with_masked_values(self):
        sql = (
            "SELECT f.\"fact id\" AS id, f.\"raw value\" AS rv, s.note AS note"
            " FROM fact_lines f LEFT JOIN shipment_lines s"
            "   ON s.marker = f.marker"
            " ORDER BY id, note"
        )
        self.assert_same_rows("mixed", sql, ordered=True)

    def test_correlated_exists_sees_only_authorized_rows(self):
        sql = (
            "SELECT \"ship id\" FROM shipment_lines s"
            " WHERE EXISTS (SELECT 1 FROM fact_lines f"
            "              WHERE f.marker = s.marker)"
            " ORDER BY \"ship id\""
        )
        self.assert_same_rows("alice", sql, ordered=True)
        self.assert_same_rows("mixed", sql, ordered=True)

    def test_hidden_value_cannot_be_recovered_via_case_or_arithmetic(self):
        # Predicates on the hidden column are evaluated on NULL; nothing
        # leaks through CASE, arithmetic, TYPEOF or COALESCE either.
        sql = (
            "SELECT \"ship id\","
            " CASE WHEN amount IS NULL THEN 'null' ELSE 'value' END AS state,"
            " COALESCE(amount, -1) AS coerced, TYPEOF(amount) AS t"
            " FROM shipment_lines ORDER BY \"ship id\""
        )
        self.assert_same_rows("sally", sql, ordered=True)
        self.assert_same_rows("betty", sql, ordered=True)

    def test_union_all_and_cte_over_masked_rows(self):
        sql = (
            "WITH per_rel AS ("
            "  SELECT 'f' AS src, COUNT(*) AS n, COUNT(\"raw value\") AS v"
            "    FROM fact_lines"
            "  UNION ALL"
            "  SELECT 's', COUNT(*), COUNT(amount) FROM shipment_lines"
            ") SELECT * FROM per_rel ORDER BY src"
        )
        self.assert_same_rows("sally", sql, ordered=True)
        self.assert_same_rows("alice", sql, ordered=True)


class TypingTest(ServiceTestCase):
    def test_blob_round_trips_and_typeof_is_preserved(self):
        report = self.query(
            "alice",
            "SELECT payload, TYPEOF(payload) AS t FROM shipment_lines"
            " ORDER BY \"ship id\"")
        kinds = [(row[0].kind, row[1].value) for row in report.rows]
        self.assertEqual(kinds, [("b", "blob"), ("b", "blob")])
        self.assertEqual(report.rows[0][0].value, b"\x00\xff")
        self.assertEqual(report.rows[1][0].value, b"")  # empty blob stays blob

    def test_big_integer_keeps_full_precision(self):
        report = self.query(
            "alice",
            "SELECT amount, amount + 1 AS plus_one FROM shipment_lines"
            " WHERE \"ship id\" = 5")
        self.assertEqual(report.rows[0][0].value, 9007199254740993)
        self.assertEqual(report.rows[0][1].value, 9007199254740994)
        encoded = report.rows[0][0].encode()
        self.assertEqual(encoded, {"t": "int", "v": "9007199254740993"})

    def test_affinity_matches_same_shape_small_database(self):
        # INTEGER-affinity column compared with a text literal: SQLite
        # converts the literal; the result must match the oracle exactly.
        sql = "SELECT \"ship id\" FROM shipment_lines WHERE amount = '100'"
        self.assert_same_rows("alice", sql)
        # TEXT-affinity column compared with a number: same story.
        sql = "SELECT \"ship id\" FROM shipment_lines WHERE note = 100"
        self.assert_same_rows("alice", sql)
        # mixed storage classes in one column keep their typeof
        sql = ("SELECT TYPEOF(amount) AS t, COUNT(*) AS n FROM shipment_lines"
               " GROUP BY t ORDER BY t")
        self.assert_same_rows("alice", sql, ordered=True)

    def test_null_int_real_text_blob_distinct_in_results(self):
        report = self.query(
            "alice",
            "SELECT NULL AS n, 1 AS i, 1.0 AS r, '1' AS t, x'31' AS b"
            " FROM fact_lines LIMIT 1")
        row = report.rows[0]
        self.assertEqual([v.kind for v in row], ["n", "i", "f", "t", "b"])
        self.assertEqual(row[1].value, 1)
        self.assertEqual(row[2].value, 1.0)
        self.assertEqual(row[3].value, "1")
        self.assertEqual(row[4].value, b"1")


class QueryShapeTest(ServiceTestCase):
    def test_cte_shadowing_physical_name_runs_on_public_data(self):
        report = self.query(
            "alice",
            "WITH facts AS (SELECT concept, marker FROM fact_lines)"
            " SELECT * FROM facts ORDER BY concept")
        self.assert_same_rows("alice",
                              "WITH facts AS (SELECT concept, marker FROM fact_lines)"
                              " SELECT * FROM facts ORDER BY concept",
                              ordered=True)
        self.assertEqual(report.relations, ["fact_lines"])

    def test_double_quoted_identifiers_with_spaces(self):
        sql = ('SELECT f."fact id" AS "the id" FROM fact_lines f'
               ' ORDER BY "the id"')
        report = self.assert_same_rows("alice", sql, ordered=True)
        self.assertEqual(report.columns, ["the id"])

    def test_empty_result_has_columns_but_no_rows(self):
        report = self.query("alice",
                            "SELECT concept FROM fact_lines WHERE 1 = 0")
        self.assertEqual(report.columns, ["concept"])
        self.assertEqual(report.rows, [])

    def test_unpublished_column_does_not_exist(self):
        # facts.decimals is a real source column but not published.
        with self.assertRaises(QueryExecutionError):
            self.query("alice", "SELECT decimals FROM fact_lines")
        with self.assertRaises(QueryExecutionError):
            self.query("alice", "SELECT * FROM fact_lines WHERE decimals = '2'")

    def test_same_named_fields_require_qualification(self):
        # Both relations publish a field named "marker"; an unqualified
        # reference in a join is an ambiguity error, not a silent choice.
        with self.assertRaises(QueryExecutionError):
            self.query("alice",
                       "SELECT marker FROM fact_lines f JOIN shipment_lines s"
                       " ON f.marker = s.marker")
        report = self.query(
            "alice",
            "SELECT f.marker AS fm, s.marker AS sm FROM fact_lines f"
            " JOIN shipment_lines s ON f.\"fact id\" = s.\"ship id\"")
        self.assertEqual(len(report.rows), 1)

    def test_result_names_identity_and_relations(self):
        report = self.query(
            "mixed",
            "SELECT * FROM fact_lines f JOIN shipment_lines s"
            " ON f.marker = s.marker")
        self.assertEqual(report.identity, "mixed")
        self.assertEqual(report.relations, ["fact_lines", "shipment_lines"])

    def test_public_relation_name_with_space(self):
        document = cq.catalog_document()
        document["relations"][0]["name"] = "fact lines public"
        catalog_path = cq.write_catalog(
            os.path.join(self.tmp, "c2.json"), document)
        service = ControlledQueryService(self.db, catalog_path, self.policy_path)
        report = service.query(
            "alice", sql='SELECT "fact id" FROM "fact lines public"'
                         ' ORDER BY "fact id"')
        self.assertEqual(cq.display_rows(report), [("1",), ("3",)])


class ResultFileTest(ServiceTestCase):
    def result_path(self, name="out.json"):
        return os.path.join(self.tmp, name)

    def test_result_file_round_trip(self):
        path = self.result_path()
        self.service.query(
            "alice", sql="SELECT * FROM shipment_lines ORDER BY \"ship id\"",
            result_path=path)
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["format"], "controlled-query-result")
        self.assertEqual(document["identity"], "alice")
        self.assertEqual(document["relations"], ["shipment_lines"])
        self.assertEqual(document["row_count"], 2)
        # BLOB is base64 and reversible; the big integer is decimal text.
        first_payload = document["rows"][0][3]
        self.assertEqual(first_payload, {"t": "blob", "v": "AP8="})
        big_amount = document["rows"][1][1]
        self.assertEqual(big_amount, {"t": "int", "v": "9007199254740993"})
        self.assertEqual(document["columns"],
                         ["ship id", "amount", "note", "payload", "marker"])

    def test_result_file_never_contains_hidden_values(self):
        path = self.result_path()
        self.service.query("sally", sql="SELECT * FROM shipment_lines",
                           result_path=path)
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("200", raw)      # hidden amount never appears
        self.assertNotIn("x'01'", raw)
        self.assertNotIn("AQ==", raw)     # base64 of the hidden BLOB
        document = json.loads(raw)
        self.assertEqual(document["rows"][0][1], {"t": "null"})
        self.assertEqual(document["rows"][0][3], {"t": "null"})

    def test_refuses_to_overwrite_without_flag(self):
        path = self.result_path()
        self.service.query("alice", sql="SELECT 1 AS one", result_path=path)
        with self.assertRaises(InvocationError):
            self.service.query("alice", sql="SELECT 2 AS one", result_path=path)
        # the original file is untouched
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["rows"][0][0], {"t": "int", "v": "1"})
        self.service.query("alice", sql="SELECT 2 AS one", result_path=path,
                           overwrite=True)
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["rows"][0][0], {"t": "int", "v": "2"})

    def test_failed_query_leaves_no_result_file(self):
        path = self.result_path()
        with self.assertRaises(QueryExecutionError):
            self.service.query("alice", sql="SELECT no_such_col FROM fact_lines",
                               result_path=path)
        self.assertFalse(os.path.exists(path))
        leftovers = [f for f in os.listdir(self.tmp) if f.startswith(".controlled-query-")]
        self.assertEqual(leftovers, [])


class SnapshotConsistencyTest(ServiceTestCase):
    def test_query_straddling_a_commit_sees_one_read_point(self):
        db = os.path.join(self.tmp, "wal.sqlite")
        cq.build_warehouse(db, wal=True)
        service = ControlledQueryService(db, self.catalog_path, self.policy_path)

        snapshot_taken = threading.Event()
        writer_committed = threading.Event()
        writer_errors = []

        def hook():
            # The reader's snapshot is established; let the writer commit
            # before any business row is read.
            snapshot_taken.set()
            self.assertTrue(writer_committed.wait(timeout=30))

        def writer():
            try:
                self.assertTrue(snapshot_taken.wait(timeout=30))
                conn = sqlite3.connect(db, isolation_level=None)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE facts SET language = 'v-new'")
                    conn.execute("UPDATE shipments SET marker = 'v-new'")
                    conn.execute("COMMIT")
                finally:
                    conn.close()
            except BaseException as exc:  # pragma: no cover - diagnostic
                writer_errors.append(exc)
            finally:
                writer_committed.set()

        sql = ("SELECT f.marker AS fm, s.marker AS sm"
               " FROM fact_lines f CROSS JOIN shipment_lines s")
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        report = service.query("alice", sql=sql, snapshot_hook=hook)
        thread.join(timeout=30)
        self.assertEqual(writer_errors, [])
        markers = {tuple(v.value for v in row) for row in report.rows}
        # The query straddled the commit but sees only the pre-commit state:
        # both markers come from the same read point, never a mix of two
        # commits.
        self.assertEqual(markers, {("en", "m-old"), (None, "m-old")})
        # The next query sees the new commit on both relations.
        report = service.query("alice", sql=sql)
        markers = {tuple(v.value for v in row) for row in report.rows}
        self.assertEqual(markers, {("v-new", "v-new")})


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cq_cli_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "wh.sqlite")
        cq.build_warehouse(self.db)
        self.catalog = cq.write_catalog(os.path.join(self.tmp, "c.json"))
        self.policy = cq.write_policy(os.path.join(self.tmp, "p.json"))
        self.project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def cli(self, *args, cwd=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = self.project
        return subprocess.run(
            [sys.executable, "-m", "controlled_query.cli", *args],
            cwd=cwd or self.tmp, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def run_cli(self, identity, sql, *extra):
        return self.cli("run", self.db, "--catalog", self.catalog,
                        "--policy", self.policy, "--identity", identity,
                        "--sql", sql, *extra)

    def test_run_ok_exit_0(self):
        result_path = os.path.join(self.tmp, "r.json")
        proc = self.run_cli("alice", "SELECT COUNT(*) AS c FROM fact_lines",
                            "--result", result_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("identity  : alice", proc.stdout)
        with open(result_path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["rows"][0][0], {"t": "int", "v": "2"})

    def test_json_stdout(self):
        proc = self.run_cli("alice", "SELECT 1 AS one", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = json.loads(proc.stdout)
        self.assertEqual(document["identity"], "alice")
        self.assertEqual(document["rows"], [[{"t": "int", "v": "1"}]])

    def test_sql_file_input(self):
        sql_path = os.path.join(self.tmp, "q.sql")
        with open(sql_path, "w", encoding="utf-8") as handle:
            handle.write('SELECT COUNT(*) AS c FROM fact_lines\n')
        proc = self.cli("run", self.db, "--catalog", self.catalog,
                        "--policy", self.policy, "--identity", "alice",
                        "--sql-file", sql_path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = json.loads(proc.stdout)
        self.assertEqual(document["rows"], [[{"t": "int", "v": "2"}]])

    def test_unknown_identity_exit_2(self):
        proc = self.run_cli("mallory", "SELECT 1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown identity", proc.stderr)

    def test_unsupported_sql_exit_4(self):
        proc = self.run_cli("alice", "SELECT * FROM facts")
        self.assertEqual(proc.returncode, 4)
        self.assertIn("not a public relation", proc.stderr)
        proc = self.run_cli("alice", "SELECT 1; SELECT 2")
        self.assertEqual(proc.returncode, 4)

    def test_execution_error_exit_5(self):
        proc = self.run_cli("alice", "SELECT decimals FROM fact_lines")
        self.assertEqual(proc.returncode, 5)
        self.assertIn("decimals", proc.stderr)

    def test_config_error_exit_3(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO lanes VALUES ('EU', 'H1', 'OP2')")
        conn.commit()
        conn.close()
        proc = self.run_cli("alice", "SELECT 1")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("not unique", proc.stderr)

    def test_failed_run_writes_no_result_file(self):
        result_path = os.path.join(self.tmp, "r.json")
        proc = self.run_cli("alice", "SELECT * FROM facts",
                            "--result", result_path)
        self.assertEqual(proc.returncode, 4)
        self.assertFalse(os.path.exists(result_path))

    def test_check_config(self):
        proc = self.cli("check-config", self.db, "--catalog", self.catalog,
                        "--policy", self.policy)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("configuration OK", proc.stdout)
        self.assertIn("fact_lines", proc.stdout)

    def test_check_config_broken_catalog_exit_3(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO lanes VALUES ('EU', 'H1', 'OP2')")
        conn.commit()
        conn.close()
        proc = self.cli("check-config", self.db, "--catalog", self.catalog,
                        "--policy", self.policy)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("not unique", proc.stderr)


if __name__ == "__main__":
    unittest.main()
