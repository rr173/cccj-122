#!/usr/bin/env python3
"""Demonstration of provenance-carrying relational queries.

The script teaches the algebra on two tiny tables, then runs it against a
**real fact warehouse built by the existing XBRL ingester** and proves that
graph leaves point at actual warehouse rows.

Everything checked here is written by hand below; nothing is derived by
calling the provenance engine to compute its own expectation.  Independent
oracles are raw SQLite runs (for result multisets) and physical row deletion
in a second database copy (for exclusion results).

Run:  python3 -I demo/run_provenance_query_demo.py
Exit 0 only if every check holds.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)

from provenance_query import (  # noqa: E402
    ProvenanceQueryBuilder,
    ProvenanceResultReader,
    TyValue,
    evaluate_exclusion,
)
from xbrl_ingest import XbrlIngestor  # noqa: E402

DICTIONARY = os.path.join(HERE, "xbrl_fixtures", "concepts.json")
FIXTURES = os.path.join(HERE, "xbrl_fixtures")


class Checker:
    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.checks = 0
        self.failures: list = []

    def check(self, label, actual, expected):
        self.checks += 1
        if actual != expected:
            self.failures.append(label)
            print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
        elif self.verbose:
            print(f"  ok   {label} = {actual!r}")

    def check_true(self, label, condition, detail=""):
        self.checks += 1
        if not condition:
            self.failures.append(label + (" " + detail if detail else ""))
            print(f"  FAIL {label} {detail}")
        elif self.verbose:
            print(f"  ok   {label}")

    def section(self, title):
        print(f"\n== {title} ==")

    def finish(self):
        if self.failures:
            print(f"\n{len(self.failures)} of {self.checks} checks FAILED")
            return 1
        print(f"\nall {self.checks} checks held")
        return 0


def multiset_from_reader(reader):
    out = {}
    for row in reader.rows():
        key = tuple(v.key() for v in row.values)
        out[key] = out.get(key, 0) + row.multiplicity
    return out


def sqlite_multiset(db_path, sql):
    conn = sqlite3.connect(db_path)
    conn.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
    try:
        out = {}
        for row in conn.execute(sql).fetchall():
            key = tuple(TyValue.from_sqlite(v).key() for v in row)
            out[key] = out.get(key, 0) + 1
        return out
    finally:
        conn.close()


def build(workdir, db_path, name, sql):
    sql_path = os.path.join(workdir, name + ".sql")
    with open(sql_path, "w", encoding="utf-8") as handle:
        handle.write(sql)
    result_path = os.path.join(workdir, name + ".sqlite")
    report = ProvenanceQueryBuilder(db_path).build(sql_path, result_path)
    return report, result_path


def exclusion_multiset(result_path, identities):
    outcome = evaluate_exclusion(result_path, identities)
    out = {}
    for row in outcome["rows"]:
        if row["after"] == 0:
            continue
        key = tuple(TyValue.decode(v).key() for v in row["values"])
        out[key] = out.get(key, 0) + row["after"]
    return out


def deletion_multiset(db_path, sql, deletions):
    copy_path = db_path + ".deleted"
    shutil.copyfile(db_path, copy_path)
    conn = sqlite3.connect(copy_path)
    try:
        for table, pks in deletions.items():
            pk_cols = [
                r[1]
                for r in sorted(
                    conn.execute(f'PRAGMA table_info("{table}")').fetchall(),
                    key=lambda r: r[5],
                )
                if r[5] > 0
            ]
            for values in pks:
                conn.execute(
                    f'DELETE FROM "{table}" WHERE '
                    + " AND ".join(f'"{c}" = ?' for c in pk_cols),
                    values,
                )
        conn.commit()
    finally:
        conn.close()
    try:
        return sqlite_multiset(copy_path, sql)
    finally:
        os.unlink(copy_path)


# ---------------------------------------------------------------------------
# Part 1: the algebra on two tiny tables
# ---------------------------------------------------------------------------


def tiny_tables(workdir, checker):
    checker.section("Tiny tables: understanding (a1+a2)*(b1+b2)")
    db = os.path.join(workdir, "tiny.sqlite")
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE left_t(id INTEGER PRIMARY KEY, v TEXT);
    CREATE TABLE right_t(id INTEGER PRIMARY KEY, v TEXT);
    INSERT INTO left_t VALUES (1,'a1'), (2,'a2');
    INSERT INTO right_t VALUES (1,'b1'), (2,'b2');
    """)
    conn.commit()
    conn.close()

    sql = "SELECT 'same' AS v FROM left_t CROSS JOIN right_t"
    report, result = build(workdir, db, "tiny_cross", sql)
    reader = ProvenanceResultReader(result)
    rows = reader.rows()
    checker.check("result tuples", len(rows), 1)
    checker.check("original multiplicity", rows[0].multiplicity, 4)
    checker.check(
        "same as plain SQLite", multiset_from_reader(reader), sqlite_multiset(db, sql)
    )

    graph = reader.graph()
    identities = {b.table + ":" + b.pk_values[0].display(): b for b in reader.base_identities()}
    a1, a2 = identities["left_t:1"], identities["left_t:2"]
    b1, b2 = identities["right_t:1"], identities["right_t:2"]

    # The expression, evaluated by hand (never by the engine) for all 16
    # exclusion subsets.
    all_ok = True
    for mask in range(16):
        excluded = frozenset(
            token for bit, token in enumerate(
                [a1.token, a2.token, b1.token, b2.token]
            ) if mask & (1 << bit)
        )
        kept = {
            "a1": 0 if a1.token in excluded else 1,
            "a2": 0 if a2.token in excluded else 1,
            "b1": 0 if b1.token in excluded else 1,
            "b2": 0 if b2.token in excluded else 1,
        }
        hand = (kept["a1"] + kept["a2"]) * (kept["b1"] + kept["b2"])
        if graph.evaluate(rows[0].graph_root, excluded) != hand:
            all_ok = False
            checker.check(
                f"subset {kept}",
                graph.evaluate(rows[0].graph_root, excluded), hand,
            )
    checker.check_true("expression == (a1+a2)*(b1+b2) for all 16 subsets", all_ok)

    # Show the structure, not only the final row count.
    token_nodes = [n for n in range(graph.node_count) if graph.kind(n) == "token"]
    add_nodes = [n for n in range(graph.node_count) if graph.kind(n) == "add"]
    mul_nodes = [n for n in range(graph.node_count) if graph.kind(n) == "mul"]
    checker.check("token leaves", len(token_nodes), 4)
    # Four alternative derivations need three additions; four products need
    # four multiplications.  The whole tuple collapses to one row whose
    # annotation is the sum of the four products.
    checker.check("addition nodes", len(add_nodes), 3)
    checker.check("multiplication nodes", len(mul_nodes), 4)
    checker.check("graph nodes", graph.node_count, 13)
    checker.check("graph edges", graph.edge_count, 18)
    print("  graph root expression (shared DAG):")
    print("    " + graph.describe(rows[0].graph_root))
    print("  leaves:")
    for token in reader.base_identities():
        print(f"    token {token.token}: {token.describe()}")

    # Exclusions.
    only_a1 = exclusion_multiset(result, ["left_t(id=1)"])
    checker.check("exclude a1 total", sum(only_a1.values()), 2)
    checker.check(
        "exclude a1 matches physical deletion",
        only_a1,
        deletion_multiset(db, sql, {"left_t": [(1,)]}),
    )
    both = exclusion_multiset(result, ["left_t(id=1)", "right_t(id=1)"])
    checker.check("exclude a1+b1 total", sum(both.values()), 1)
    checker.check(
        "exclude a1+b1 matches physical deletion",
        both,
        deletion_multiset(db, sql, {"left_t": [(1,)], "right_t": [(1,)]}),
    )
    reader.close()

    # UNION ALL of the same branch twice -> 8; exclude a1 -> 4; a1+b1 -> 2.
    sql_union = (
        "SELECT 'same' AS v FROM left_t CROSS JOIN right_t"
        " UNION ALL SELECT 'same' AS v FROM left_t CROSS JOIN right_t"
    )
    _, result_union = build(workdir, db, "tiny_union", sql_union)
    reader = ProvenanceResultReader(result_union)
    checker.check("UNION ALL branch twice multiplicity", reader.rows()[0].multiplicity, 8)
    checker.check(
        "UNION ALL matches plain SQLite",
        multiset_from_reader(reader), sqlite_multiset(db, sql_union),
    )
    checker.check(
        "UNION ALL exclude a1 total",
        sum(exclusion_multiset(result_union, ["left_t(id=1)"]).values()), 4,
    )
    checker.check(
        "UNION ALL exclude a1+b1 total",
        sum(
            exclusion_multiset(
                result_union, ["left_t(id=1)", "right_t(id=1)"]
            ).values()
        ),
        2,
    )
    reader.close()

    # Self CROSS JOIN -> (a1+a2)^2 = 4; exclude a1 -> 1 (the shared token is
    # squared; it is NOT a set that would collapse 4 into 2).
    sql_self = "SELECT 'same' AS v FROM left_t CROSS JOIN left_t AS l2"
    _, result_self = build(workdir, db, "tiny_self", sql_self)
    reader = ProvenanceResultReader(result_self)
    checker.check("self CROSS JOIN multiplicity", reader.rows()[0].multiplicity, 4)
    checker.check(
        "self CROSS JOIN exclude a1 total",
        sum(exclusion_multiset(result_self, ["left_t(id=1)"]).values()), 1,
    )
    checker.check(
        "self CROSS JOIN matches physical deletion",
        exclusion_multiset(result_self, ["left_t(id=1)"]),
        deletion_multiset(db, sql_self, {"left_t": [(1,)]}),
    )
    reader.close()


# ---------------------------------------------------------------------------
# Part 2: a real fact warehouse built by the XBRL ingester
# ---------------------------------------------------------------------------


def xbrl_demo(workdir, checker):
    checker.section("Real XBRL fact warehouse: graph leaves point at stored rows")
    db = os.path.join(workdir, "xbrl.sqlite")
    ingestor = XbrlIngestor(db, DICTIONARY)
    reports = {}
    for name in ("demo-A.xml", "demo-B.xml", "demo-C.xml", "demo-D.xml"):
        reports[name] = ingestor.ingest_file(os.path.join(FIXTURES, name))
    ingestor.close()
    # Hand-checked from the fixtures: these four files hold these fact counts.
    checker.check("demo-A facts", reports["demo-A.xml"].fact_count, 8)
    checker.check("demo-B facts", reports["demo-B.xml"].fact_count, 5)
    checker.check("demo-C facts", reports["demo-C.xml"].fact_count, 2)
    checker.check("demo-D facts", reports["demo-D.xml"].fact_count, 4)

    sql = """
    SELECT f.concept AS concept,
           c.entity_identifier AS entity,
           s.source_id AS source
    FROM facts AS f
    JOIN sources AS s   ON s.source_id = f.source_id
    JOIN contexts AS c  ON c.context_digest = f.context_digest
    WHERE f.concept_type = 'decimal'
    """
    report, result = build(workdir, db, "xbrl_facts", sql)
    reader = ProvenanceResultReader(result)
    engine = multiset_from_reader(reader)
    checker.check("XBRL result matches plain SQLite", engine, sqlite_multiset(db, sql))
    checker.check("XBRL token count", report.tokens, 27)
    print(f"  distinct result tuples: {report.result_rows}")
    print(f"  graph nodes / edges   : {report.graph_nodes} / {report.graph_edges}")

    # Every graph leaf must resolve to a real warehouse row.
    conn = sqlite3.connect(db)
    try:
        leaves_ok = True
        checked = 0
        for identity in reader.base_identities():
            table = identity.table
            where = " AND ".join(f'"{c}" = ?' for c in identity.pk_columns)
            values = tuple(v.value for v in identity.pk_values)
            count = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE {where}', values
            ).fetchone()[0]
            if count != 1:
                leaves_ok = False
                print(f"    MISSING {identity.describe()}")
            checked += 1
        checker.check_true("all graph leaves resolve to real rows", leaves_ok)
        checker.check("leaf count", checked, 27)

        # Prove they are not invented demo IDs: facts leaves point at the
        # actual fact rows produced by the ingester, with matching concepts.
        facts_leaves = [b for b in reader.base_identities() if b.table == "facts"]
        concepts_seen = set()
        for leaf in facts_leaves:
            row = conn.execute(
                "SELECT concept FROM facts WHERE id = ?",
                (leaf.pk_values[0].value,),
            ).fetchone()
            concepts_seen.add(row[0])
        checker.check_true(
            "facts leaves cover the warehouse concepts",
            "{" + "http://example.com/co" + "}Revenue" in concepts_seen,
        )
        source_ids = {r[0] for r in conn.execute("SELECT source_id FROM sources")}
        source_leaves = {
            b.pk_values[0].value for b in reader.base_identities()
            if b.table == "sources"
        }
        checker.check_true(
            "sources leaves are the warehouse's own source ids",
            source_leaves <= source_ids and len(source_leaves) == len(source_ids),
            f"{sorted(source_leaves)}",
        )
        context_leaves = {
            b.pk_values[0].value for b in reader.base_identities()
            if b.table == "contexts"
        }
        context_digests = {
            r[0] for r in conn.execute("SELECT context_digest FROM contexts")
        }
        checker.check_true(
            "contexts leaves are the warehouse's own digests",
            context_leaves <= context_digests,
        )
    finally:
        conn.close()

    # Exclude one source entirely: only facts from that source disappear.
    a_source = reports["demo-A.xml"].source_id
    excluded = exclusion_multiset(result, [f"sources(source_id='{a_source}')"])
    expected = deletion_multiset(
        db, sql, {"sources": [(a_source,)]}
    )
    checker.check(
        "excluding a source matches physical deletion", excluded, expected
    )
    leftovers = {
        row[0] for row in sqlite3.connect(db).execute(
            "SELECT DISTINCT source_id FROM facts"
        )
    }
    checker.check_true(
        "source exclusion removes only that source's rows",
        all(
            key[2] != "t:" + a_source
            for key in excluded
        ),
    )
    reader.close()


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="pq_demo_")
    checker = Checker()
    try:
        tiny_tables(workdir, checker)
        xbrl_demo(workdir, checker)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return checker.finish()


if __name__ == "__main__":
    raise SystemExit(main())
