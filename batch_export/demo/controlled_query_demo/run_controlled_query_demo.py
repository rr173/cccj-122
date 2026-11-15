#!/usr/bin/env python3
"""Acceptance demo for the controlled analytical query entry point.

Story line: *the same query under a different identity computes over a
genuinely different set of relations.*

Stages:

1.  Build a real fact warehouse with the existing XBRL ingestor: two
    entities share the identifier ``ACME-7`` under different schemes, plus
    a third entity nobody may see.  Add business tables whose owner path
    has a composite key and two hops, with NULL keys, unmatched keys,
    same-named fields and BLOBs.
2.  Validate the administrator's catalog + policy (``check-config``).
3.  For every identity, dump both public relations and compare row-for-row
    with hand-written expectations.
4.  Run the demo queries: the service's answers must equal the independent
    plain-SQLite oracle (``plain_sqlite_oracle.py``) computed from the
    hand-written authorized rows -- never from the service itself.
5.  Break the uniqueness of a composite target key: the whole query fails
    (no duplicated details, no arbitrary owner); restore it and re-run.
6.  Fire the rejection battery through the CLI and prove non-zero exits
    with no sensitive values and no partial result files.
7.  Inspect a result file: identity, relations, typed values; hidden data
    is absent.
8.  Concurrency: a writer commits a two-table marker change while a query
    straddles the commit; the query still sees one read point, and the
    next query sees the new commit.

Run:  python3 -I demo/controlled_query_demo/run_controlled_query_demo.py
Exit 0 only if every check holds.
"""
from __future__ import annotations

import json
import os
import shutil
import site
import sqlite3
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
for _path in (PROJECT, HERE, site.getusersitepackages()):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import expectations as exp  # noqa: E402
import plain_sqlite_oracle as oracle  # noqa: E402
from controlled_query import ConfigError, ControlledQueryService  # noqa: E402
from xbrl_ingest import XbrlIngestor  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
DICTIONARY = os.path.join(PROJECT, "demo", "xbrl_fixtures", "concepts.json")

SCHEME_LEI, SCHEME_NAT = exp.SCHEME_LEI, exp.SCHEME_NAT
E1, E2, E3 = exp.E1, exp.E2, exp.E3


class Checker:
    def __init__(self):
        self.checks = 0
        self.failures = []

    def check(self, label, actual, expected):
        self.checks += 1
        if actual != expected:
            self.failures.append(label)
            print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")
        else:
            print(f"  ok   {label}")

    def check_true(self, label, condition, detail=""):
        self.checks += 1
        if not condition:
            self.failures.append(label)
            print(f"  FAIL {label} {detail}")
        else:
            print(f"  ok   {label}")

    def section(self, title):
        print(f"\n== {title} ==")

    def finish(self):
        if self.failures:
            print(f"\n{len(self.failures)} of {self.checks} checks FAILED")
            return 1
        print(f"\nall {self.checks} checks held")
        return 0


# ---------------------------------------------------------------------------
# Warehouse construction
# ---------------------------------------------------------------------------

BUSINESS_DDL = """
CREATE TABLE operators (
    op_code TEXT,
    scheme TEXT,
    identifier TEXT,
    marker TEXT
);
CREATE TABLE lanes (
    region TEXT,
    hub TEXT,
    op_code TEXT
);
CREATE TABLE shipments (
    ship_id INTEGER PRIMARY KEY,
    lane_region TEXT,
    lane_hub TEXT,
    amount INTEGER,
    note TEXT,
    payload BLOB,
    marker TEXT
);
"""

SHIPMENT_ROWS = [
    (1, "EU", "H1", 100, "fragile", b"\x00\xff\x01", "m1"),
    (2, "EU", "H2", 200, "heavy", b"\x02", "m1"),
    (3, None, "H1", 300, "no lane key", None, "m1"),   # NULL left key
    (4, "AP", "H9", 400, "unknown lane", None, "m1"),  # unmatched lane
    (5, "EU", "H1", 9007199254740993, "big", b"", "m1"),
    (6, "EU", "H1", 50, "linked", b"\x06", "en"),
]


def build_warehouse(db_path):
    ingestor = XbrlIngestor(db_path, DICTIONARY)
    for name in ("acme_lei", "acme_nat", "other_lei"):
        report = ingestor.ingest_file(os.path.join(FIXTURES, name + ".xml"))
        assert report.fact_count > 0, name
    ingestor.close()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(BUSINESS_DDL)
    conn.executemany(
        "INSERT INTO operators VALUES (?,?,?,?)",
        [("OP-LEI", SCHEME_LEI, "ACME-7", "m1"),
         ("OP-NAT", SCHEME_NAT, "ACME-7", "m1")])
    conn.executemany(
        "INSERT INTO lanes VALUES (?,?,?)",
        [("EU", "H1", "OP-LEI"), ("EU", "H2", "OP-NAT")])
    conn.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?)",
                     SHIPMENT_ROWS)
    conn.commit()
    conn.close()


def catalog_document():
    return {
        "version": 1,
        "relations": [
            {
                "name": "fact_lines",
                "source": "facts",
                "fields": [
                    {"name": "fact id", "column": "id"},
                    {"name": "concept"},
                    {"name": "raw value", "column": "value_text"},
                    {"name": "marker", "column": "language"},
                ],
                "owner": {
                    "path": [
                        {"table": "contexts",
                         "left": ["context_digest"],
                         "right": ["context_digest"]}
                    ],
                    "entity": {"scheme": "entity_scheme",
                               "identifier": "entity_identifier"},
                },
                "summary_hidden": ["raw value"],
            },
            {
                "name": "shipment_lines",
                "source": "shipments",
                "fields": [
                    {"name": "ship id", "column": "ship_id"},
                    {"name": "amount"},
                    {"name": "note"},
                    {"name": "payload"},
                    {"name": "marker"},
                ],
                "owner": {
                    "path": [
                        {"table": "lanes",
                         "left": ["lane_region", "lane_hub"],
                         "right": ["region", "hub"]},
                        {"table": "operators",
                         "left": ["op_code"],
                         "right": ["op_code"]},
                    ],
                    "entity": {"scheme": "scheme", "identifier": "identifier"},
                },
                "summary_hidden": ["amount", "payload"],
            },
        ],
    }


def policy_document():
    return {
        "version": 1,
        "identities": [
            {"name": "ava", "groups": []},
            {"name": "noah", "groups": []},
            {"name": "sara", "groups": []},
            {"name": "gwen", "groups": ["g-full", "g-sum"]},
            {"name": "dana", "groups": ["g-deny"]},
            {"name": "nina", "groups": []},
            {"name": "mona", "groups": []},
        ],
        "grants": [
            {"subject": "ava", "scheme": E1[0], "identifier": E1[1],
             "effect": "full"},
            {"subject": "noah", "scheme": E2[0], "identifier": E2[1],
             "effect": "full"},
            {"subject": "sara", "scheme": E2[0], "identifier": E2[1],
             "effect": "summary"},
            {"subject": "g-full", "scheme": E1[0], "identifier": E1[1],
             "effect": "full"},
            {"subject": "g-sum", "scheme": E1[0], "identifier": E1[1],
             "effect": "summary"},
            {"subject": "dana", "scheme": E1[0], "identifier": E1[1],
             "effect": "full"},
            {"subject": "g-deny", "scheme": E1[0], "identifier": E1[1],
             "effect": "deny"},
            {"subject": "mona", "scheme": E1[0], "identifier": E1[1],
             "effect": "full"},
            {"subject": "mona", "scheme": E2[0], "identifier": E2[1],
             "effect": "summary"},
        ],
    }


def write_json(path, document):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)


def service_keys(report):
    return [tuple(v.key() for v in row) for row in report.rows]


def oracle_keys(identity, sql):
    columns, rows = oracle.answer(identity, sql)
    return columns, [tuple(oracle.typed_key(v) for v in row) for row in rows]


# ---------------------------------------------------------------------------
# Demo stages
# ---------------------------------------------------------------------------

def main():
    checker = Checker()
    work = tempfile.mkdtemp(prefix="cq_demo_")
    try:
        db = os.path.join(work, "warehouse.sqlite")
        catalog_path = os.path.join(work, "catalog.json")
        policy_path = os.path.join(work, "policy.json")

        checker.section("stage 1: build a real warehouse with the XBRL ingestor")
        build_warehouse(db)
        write_json(catalog_path, catalog_document())
        write_json(policy_path, policy_document())
        conn = sqlite3.connect(db)
        fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        entities = conn.execute(
            "SELECT DISTINCT entity_scheme, entity_identifier FROM contexts"
            " ORDER BY 1, 2").fetchall()
        conn.close()
        checker.check("facts ingested", fact_count, 6)
        checker.check("entities present", entities,
                      [(SCHEME_LEI, "ACME-7"), (SCHEME_LEI, "OTHER-9"),
                       (SCHEME_NAT, "ACME-7")])
        print("  note: two entities share identifier 'ACME-7' under"
              " different schemes; the tuple (scheme, identifier) is the"
              " identity, never a concatenated string")

        service = ControlledQueryService(db, catalog_path, policy_path)

        checker.section("stage 2: check-config validates catalog + policy + schema")
        report = service.check_config()
        checker.check("relations validated",
                      [r[0] for r in report.relations],
                      ["fact_lines", "shipment_lines"])

        checker.section("stage 3: every identity's relations match hand-written rows")
        for identity in exp.EXPECTED:
            for relation in ("fact_lines", "shipment_lines"):
                sql = f'SELECT * FROM "{relation}"'
                service_report = service.query(identity, sql=sql)
                _, expected = oracle_keys(identity, sql)
                checker.check(f"{identity}.{relation} rows",
                              sorted(service_keys(service_report)),
                              sorted(expected))

        checker.section("stage 4: same query, different identity, different relations")
        for name, (sql, identities, _ordered) in exp.QUERIES.items():
            for identity in identities:
                service_report = service.query(identity, sql=sql)
                expected_columns, expected_rows = oracle_keys(identity, sql)
                checker.check(f"{name} [{identity}] columns",
                              service_report.columns, expected_columns)
                checker.check(f"{name} [{identity}] rows",
                              service_keys(service_report), expected_rows)

        checker.section("stage 5: duplicate composite target key fails the whole query")
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO lanes VALUES ('EU', 'H1', 'OP-NAT')")
        conn.commit()
        conn.close()
        try:
            service.query("ava", sql="SELECT * FROM fact_lines")
            checker.check_true("duplicate target key rejected", False,
                               "query unexpectedly succeeded")
        except ConfigError as exc:
            checker.check_true("duplicate target key rejected",
                               "not unique" in str(exc), str(exc))
            checker.check_true("error locates the config",
                               "shipment_lines" in str(exc)
                               and "lanes" in str(exc), str(exc))
        conn = sqlite3.connect(db)
        duplicated = conn.execute(
            "SELECT COUNT(*) FROM shipments").fetchone()[0]
        conn.execute("DELETE FROM lanes WHERE region='EU' AND hub='H1'"
                     " AND op_code='OP-NAT'")
        conn.commit()
        conn.close()
        checker.check("no shipment rows were duplicated by the failure",
                      duplicated, len(SHIPMENT_ROWS))
        recovered = service.query("ava", sql="SELECT COUNT(*) AS c FROM fact_lines")
        checker.check("query works again after restoring uniqueness",
                      [tuple(v.value for v in r) for r in recovered.rows],
                      [(3,)])

        checker.section("stage 6: rejection battery (CLI, exit codes, no leaks)")
        cli_cases = [
            ("main-qualified physical table", "SELECT * FROM main.facts", 4),
            ("metadata probing", "SELECT * FROM sqlite_master", 4),
            ("metadata probing (sqlite_schema)", "SELECT * FROM sqlite_schema", 4),
            ("unpublished source column",
             "SELECT value_canonical FROM fact_lines", 5),
            ("hidden-value filter via unpublished column",
             "SELECT * FROM fact_lines WHERE decimals = '2'", 5),
            ("nested CTE reaching the physical table",
             "WITH x AS (SELECT * FROM facts) SELECT * FROM x", 4),
            ("multi-statement", "SELECT 1; SELECT * FROM fact_lines", 4),
            ("dangerous function", "SELECT load_extension('/tmp/evil.so')", 4),
            ("pragma", "PRAGMA table_info(facts)", 4),
            ("table-valued function", "SELECT * FROM json_each('[1,2]')", 4),
            ("scalar subquery to physical table",
             "SELECT * FROM shipment_lines"
             " WHERE amount = (SELECT MAX(amount) FROM shipments)", 4),
            ("write statement", "DELETE FROM fact_lines", 4),
            ("attach", "ATTACH DATABASE 'other.sqlite' AS o", 4),
        ]
        sensitive = ["1000.50", "5000", "2000.75", "7000", "999",
                     "lei note", "fragile", "9007199254740993"]
        for label, sql, expected_code in cli_cases:
            result_path = os.path.join(work, "rejected.json")
            proc = subprocess.run(
                [sys.executable, "-m", "controlled_query.cli", "run", db,
                 "--catalog", catalog_path, "--policy", policy_path,
                 "--identity", "ava", "--sql", sql,
                 "--result", result_path],
                cwd=work,
                env={**os.environ, "PYTHONPATH": PROJECT},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            checker.check(f"reject {label}: exit code",
                          proc.returncode, expected_code)
            leaked = [s for s in sensitive
                      if s in proc.stdout or s in proc.stderr]
            checker.check(f"reject {label}: no sensitive values in output",
                          leaked, [])
            checker.check_true(f"reject {label}: no result file left",
                               not os.path.exists(result_path))

        checker.section("stage 7: result file content and typed encoding")
        result_path = os.path.join(work, "sara_result.json")
        sara_report = service.query(
            "sara",
            sql='SELECT "fact id", concept, "raw value" FROM fact_lines'
                ' ORDER BY "fact id"',
            result_path=result_path)
        with open(result_path, encoding="utf-8") as handle:
            document = json.load(handle)
        checker.check("result names the identity", document["identity"], "sara")
        checker.check("result names the relations", document["relations"],
                      ["fact_lines"])
        checker.check("result row count", document["row_count"], 2)
        checker.check("hidden values are SQL NULL in the file",
                      [row[2] for row in document["rows"]],
                      [{"t": "null"}, {"t": "null"}])
        with open(result_path, encoding="utf-8") as handle:
            raw = handle.read()
        checker.check_true("no hidden originals in the file",
                           all(s not in raw for s in
                               ("2000.75", "7000", "1000.50", "999")),
                           raw[:400])
        typed = service.query(
            "ava",
            sql="SELECT NULL AS n, 9007199254740993 AS big, 1.5 AS r,"
                " 'txt' AS t, x'00ff' AS b, TYPEOF(x'00ff') AS bt"
                " FROM fact_lines LIMIT 1")
        row = typed.rows[0]
        checker.check("storage classes preserved",
                      [v.kind for v in row],
                      ["n", "i", "f", "t", "b", "t"])
        checker.check("64-bit integer exact", row[1].value, 9007199254740993)
        checker.check("blob reversible", row[4].value, b"\x00\xff")
        checker.check("typeof() answer", row[5].value, "blob")
        encoded = typed.to_document()["rows"][0]
        checker.check("typed JSON encoding", encoded[:5], [
            {"t": "null"},
            {"t": "int", "v": "9007199254740993"},
            {"t": "real", "v": 1.5},
            {"t": "text", "v": "txt"},
            {"t": "blob", "v": "AP8="},
        ])

        checker.section("stage 8: one query = one read point under WAL writes")
        snapshot_taken = threading.Event()
        writer_committed = threading.Event()
        writer_errors = []

        def hook():
            snapshot_taken.set()
            if not writer_committed.wait(timeout=30):
                raise RuntimeError("writer did not commit in time")

        def writer():
            try:
                if not snapshot_taken.wait(timeout=30):
                    raise RuntimeError("reader never reached the snapshot")
                conn = sqlite3.connect(db, isolation_level=None)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE facts SET language = 'm2'")
                    conn.execute("UPDATE shipments SET marker = 'm2'")
                    conn.execute("COMMIT")
                finally:
                    conn.close()
            except BaseException as exc:  # pragma: no cover - diagnostic
                writer_errors.append(exc)
            finally:
                writer_committed.set()

        marker_sql = ("SELECT f.marker AS fm, s.marker AS sm"
                      " FROM fact_lines f CROSS JOIN shipment_lines s")
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        straddled = service.query("ava", sql=marker_sql, snapshot_hook=hook)
        thread.join(timeout=30)
        checker.check("writer committed cleanly", writer_errors, [])
        # The straddling query must equal the oracle's answer over the
        # pre-commit authorized rows: one read point, never a mix of the
        # two commits' markers.
        _, expected_pre = oracle_keys("ava", marker_sql)
        checker.check("straddling query sees exactly one read point",
                      sorted(service_keys(straddled)), sorted(expected_pre))
        markers = {tuple(v.value for v in row) for row in straddled.rows}
        checker.check_true("no marker from the new commit leaked in",
                           all("m2" not in m for m in markers), str(markers))
        after = service.query("ava", sql=marker_sql)
        after_markers = {tuple(v.value for v in row) for row in after.rows}
        checker.check("the next query sees the new commit",
                      after_markers, {("m2", "m2")})

        checker.section("summary")
        print("  identities see different computable relations for one SQL:")
        for identity in ("ava", "gwen", "sara", "nina"):
            counts = service.query(
                identity,
                sql='SELECT COUNT(*) AS f FROM fact_lines')
            print(f"    {identity:5s} fact_lines rows: "
                  f"{counts.rows[0][0].display()}")
        return checker.finish()
    finally:
        shutil.rmtree(work, True)


if __name__ == "__main__":
    raise SystemExit(main())
