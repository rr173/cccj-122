#!/usr/bin/env python3
"""Repeatable disaster-recovery drill for the SQLite fact warehouse.

This script exercises the warehouse_backup tool end to end against a small
but real XBRL fact warehouse (demo fixtures A-D: same-value duplicates, a
genuine conflict, nil, high-precision numbers):

  0. build the production warehouse (WAL mode) and list expected facts
  1. root backup while ingestion connections stay open
  2. child backup after a new source, a grandchild, and a branch off the root
  3. grow the image with an auxiliary table, shrink it with DELETE+VACUUM,
     prove an incremental restore carries no stale parent tail
  4. re-backup identical content: new id, zero new chunk files
  5. move production and the original XBRL away, restore a 3-level chain
  6. prune preview/apply keeping a grandchild and the sibling branch
  7. WAL live-writer consistency: two markers advanced in one transaction
  8. corrupted / missing shared chunk: restore fails, existing target kept
  9. prune refuses missing parents, cycles, bad fields, bad references
 10. lock coordination: a prune cannot delete chunks a restore is reading
 11. symlink substitution is refused
 12. the original ingest / query / check commands keep working

Every restore is verified independently of the backup module: whole-file
SHA-256 via hashlib, PRAGMA integrity_check via a raw sqlite3 connection,
and business queries through the original xbrl_ingest entry points.

Run from anywhere:  python3 demo/run_backup_drill.py
Exit status 0 means every check held.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "xbrl_fixtures")
sys.path.insert(0, PROJECT)

from xbrl_ingest import XbrlIngestor  # noqa: E402


class Checker:
    def __init__(self):
        self.failures: list[str] = []
        self.checks = 0

    def check(self, label: str, actual, expected) -> None:
        self.checks += 1
        if actual != expected:
            self.failures.append(f"{label}: expected {expected!r}, got {actual!r}")
            print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
        else:
            print(f"  ok {label}")

    def check_true(self, label: str, value) -> None:
        self.check(label, bool(value), True)

    def finish(self) -> int:
        if self.failures:
            print(f"\n{len(self.failures)} of {self.checks} drill checks failed")
            return 1
        print(f"\ndrill complete: all {self.checks} checks held")
        return 0


def sha256_file(path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def run_cli(checker: Checker, *argv, expect=0):
    """Run a warehouse_backup CLI command as a real subprocess."""
    result = subprocess.run(
        [sys.executable, "-m", "warehouse_backup.cli", *argv],
        cwd=PROJECT, capture_output=True, text=True)
    checker.check(f"cli {' '.join(argv[:1])} exit code", result.returncode, expect)
    if result.returncode != expect:
        print(f"  command: {argv}\n  stdout: {result.stdout}\n"
              f"  stderr: {result.stderr}")
    return result


def business_snapshot(db_path, dictionary):
    """All business coordinates through the original query entry points."""
    with XbrlIngestor(db_path, dictionary) as ing:
        coordinates = []
        for result in ing.query():
            coordinates.append({
                "status": result.status,
                "concept": result.coordinate.concept,
                "context": result.coordinate.context.canonical_text(),
                "unit": (result.coordinate.unit.canonical_text()
                         if result.coordinate.unit else None),
                "language": result.coordinate.language,
                "candidates": sorted(
                    (c.source_id, c.position, c.nil, c.value_text, c.decimals)
                    for c in result.candidates),
            })
        sources = [(s.source.source_id, s.source.content_sha256,
                    s.source.byte_size) for s in ing.sources()]
        first_source = sources[0][0]
        traced = ing.trace_fact(first_source, 1)
        return {
            "sources": sources,
            "fact_count": ing.store.fact_row_count(),
            "coordinates": coordinates,
            "traced_first": (traced.value_text, traced.location.line),
        }


def raw_integrity_check(path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return "ok" if [r[0] for r in rows] == ["ok"] else str(rows)


def manifest_of(backup_dir, backup_id):
    path = os.path.join(backup_dir, "manifests", f"{backup_id:06d}.json")
    with open(path) as handle:
        return json.load(handle)


def chunk_file_count(backup_dir) -> int:
    total = 0
    chunks = os.path.join(backup_dir, "chunks")
    for fanout in os.listdir(chunks):
        total += len(os.listdir(os.path.join(chunks, fanout)))
    return total


def main() -> int:
    checker = Checker()
    workdir = tempfile.mkdtemp(prefix="warehouse_drill_")
    backups = os.path.join(workdir, "backups")
    prod_dir = os.path.join(workdir, "production")
    os.makedirs(prod_dir)
    db = os.path.join(prod_dir, "warehouse.sqlite")
    fixtures = os.path.join(prod_dir, "instances")
    shutil.copytree(FIXTURES, fixtures)
    # The concept dictionary is configuration, not instance data: keep a copy
    # outside the production tree so it survives the production move.
    dictionary = os.path.join(workdir, "concepts.json")
    shutil.copy(os.path.join(FIXTURES, "concepts.json"), dictionary)

    try:
        # ---------------------------------------------------------- stage 0
        print("0. Build the production warehouse (WAL mode) and state"
              " expectations")
        setup = sqlite3.connect(db)
        setup.execute("PRAGMA journal_mode=WAL")
        # Baseline bulk so the warehouse has realistic size: later ingests
        # then change only a few chunks and incremental layering is visible.
        setup.execute("CREATE TABLE warehouse_bulk (id INTEGER PRIMARY KEY,"
                      " pad TEXT)")
        setup.executemany("INSERT INTO warehouse_bulk VALUES (?, ?)",
                          [(i, "b" * 1500) for i in range(2500)])
        setup.commit()
        setup.close()
        ing = XbrlIngestor(db, dictionary)
        report_a = ing.ingest_file(os.path.join(fixtures, "demo-A.xml"))
        report_b = ing.ingest_file(os.path.join(fixtures, "demo-B.xml"))
        print("  expected after demo-A + demo-B:")
        print("    sources: 2 (src_%s.., src_%s..)"
              % (report_a.source_id[4:10], report_b.source_id[4:10]))
        print("    facts: 13 (8 from A, 5 from B)")
        print("    Revenue duration: CONFLICT 1234.50 (A) vs 1250.00 (B)")
        print("    Revenue instant:  single, 3 candidates 1.0 / 1.00 / 1.000")
        print("    Nothing: nil; Zero: single 2 candidates; Huge: 40-digit"
              " decimals=INF; Tiny: 1e-12")
        print("    Note(en): CONFLICT '  retail segment  ' vs 'retail segment'")
        checker.check("stage0 source count", ing.store.source_count(), 2)
        checker.check("stage0 fact count", ing.store.fact_row_count(), 13)
        revenue = ing.query(concept_local="Revenue", period_kind="duration")
        checker.check("stage0 Revenue duration status",
                      revenue[0].status, "conflict")
        checker.check("stage0 Revenue duration values",
                      sorted(c.value_text for c in revenue[0].candidates),
                      ["1234.50", "1250.00"])
        instant = ing.query(concept_local="Revenue", period_kind="instant")
        checker.check("stage0 Revenue instant candidates",
                      len(instant[0].candidates), 3)
        checker.check("stage0 Revenue instant status",
                      instant[0].status, "single")
        nothing = ing.query(concept_local="Nothing")
        checker.check("stage0 Nothing is nil", nothing[0].candidates[0].nil,
                      True)
        huge = ing.query(concept_local="Huge")
        checker.check("stage0 Huge text", huge[0].candidates[0].value_text,
                      "1234567890123456789012345678901234567890")
        checker.check("stage0 Huge decimals", huge[0].candidates[0].decimals,
                      "INF")
        note = ing.search_text("retail segment")
        checker.check("stage0 Note conflict texts",
                      sorted(c.value_text for c in note[0].candidates),
                      ["  retail segment  ", "retail segment"])
        snap_stage0 = business_snapshot(db, dictionary)
        checker.check("stage0 journal mode stays WAL",
                      sqlite3.connect(db).execute(
                          "PRAGMA journal_mode").fetchone()[0], "wal")

        # ---------------------------------------------------------- stage 1
        print("\n1. Root backup while the ingestion connection stays open")
        result = run_cli(checker, "create", backups, "--db", db, "--json")
        created = json.loads(result.stdout)
        checker.check("root id", created["backup_id"], 1)
        checker.check("root has no parent", created["parent"], None)
        checker.check_true("root lists every chunk",
                           created["layer_chunks"] == created["chunk_count"])
        checker.check_true("root wrote chunk files",
                           created["new_chunk_files"] > 0)
        m1 = manifest_of(backups, 1)
        checker.check("root manifest chunk_size", m1["chunk_size"], 65536)
        run_cli(checker, "check", backups)
        listing = run_cli(checker, "list", backups, "--json")
        checker.check("list shows one backup",
                      len(json.loads(listing.stdout)), 1)
        # The production database was not checkpointed or altered by backup.
        checker.check("production still WAL after backup",
                      sqlite3.connect(db).execute(
                          "PRAGMA journal_mode").fetchone()[0], "wal")
        checker.check("no staging files left",
                      os.listdir(os.path.join(backups, "tmp")), [])
        # Business writes on the original connection still work.
        report_c = ing.ingest_file(os.path.join(fixtures, "demo-C.xml"))
        checker.check("ingest after backup works",
                      ing.store.source_count(), 3)

        # ---------------------------------------------------------- stage 2
        print("\n2. Child backup, grandchild, and a branch off the root")
        print("  expected after demo-C: sources 3, Assets split by entity"
              " scheme (2 coordinates, each single 100)")
        assets = ing.query(concept_local="Assets")
        checker.check("stage2 Assets coordinates", len(assets), 2)
        checker.check("stage2 Assets values",
                      sorted(a.candidates[0].value_text for a in assets),
                      ["100", "100"])
        result = run_cli(checker, "create", backups, "--db", db,
                         "--parent", "1", "--json")
        created2 = json.loads(result.stdout)
        checker.check("child id", created2["backup_id"], 2)
        checker.check("child parent", created2["parent"], 1)
        checker.check_true("child inherits most chunks",
                           0 < created2["layer_chunks"]
                           < created2["chunk_count"])

        report_d = ing.ingest_file(os.path.join(fixtures, "demo-D.xml"))
        print("  expected after demo-D: sources 4, Note(LEI-002) en CONFLICT"
              " alpha/beta, fr single alpha, de single alpha")
        checker.check("stage2 source count", ing.store.source_count(), 4)
        checker.check("stage2 fact count", ing.store.fact_row_count(), 19)
        notes_d = ing.query(concept_local="Note", entity_identifier="LEI-002")
        by_lang = {r.coordinate.language: r for r in notes_d}
        checker.check("stage2 Note en status", by_lang["en"].status, "conflict")
        checker.check("stage2 Note en texts",
                      sorted(c.value_text for c in by_lang["en"].candidates),
                      ["alpha", "beta"])
        checker.check("stage2 Note fr status", by_lang["fr"].status, "single")
        checker.check("stage2 Note de status", by_lang["de"].status, "single")
        result = run_cli(checker, "create", backups, "--db", db,
                         "--parent", "2", "--json")
        created3 = json.loads(result.stdout)
        checker.check("grandchild id", created3["backup_id"], 3)
        checker.check("grandchild parent", created3["parent"], 2)

        files_before_branch = chunk_file_count(backups)
        result = run_cli(checker, "create", backups, "--db", db,
                         "--parent", "1", "--json")
        created4 = json.loads(result.stdout)
        checker.check("branch id", created4["backup_id"], 4)
        checker.check("branch parent", created4["parent"], 1)
        checker.check("branch is same content as grandchild",
                      created4["image_sha256"], created3["image_sha256"])
        checker.check("branch wrote no new chunk files",
                      created4["new_chunk_files"], 0)
        checker.check("chunk store unchanged by branch",
                      chunk_file_count(backups), files_before_branch)
        checker.check_true("branch reuses shared chunks",
                           created4["reused_chunk_files"] > 0)
        ing.close()
        snap_stage2 = business_snapshot(db, dictionary)
        checker.check("snapshot stable across reopen",
                      snap_stage2["fact_count"], 19)

        show = run_cli(checker, "show", backups, "--id", "3", "--json")
        shown = json.loads(show.stdout)
        checker.check("show chain of 3", shown["chain"], [3, 2, 1])
        layers_used = {span["layer"] for span in shown["chunk_sources"]}
        checker.check_true("grandchild restore really uses parent layers",
                           1 in layers_used or 2 in layers_used)

        # ---------------------------------------------------------- stage 3
        print("\n3. Grow with an auxiliary table, shrink with DELETE+VACUUM")
        aux = sqlite3.connect(db)
        aux.execute("CREATE TABLE aux_bulk (id INTEGER PRIMARY KEY, pad TEXT)")
        aux.executemany("INSERT INTO aux_bulk VALUES (?, ?)",
                        [(i, "y" * 2000) for i in range(3000)])
        aux.commit()
        aux.close()
        result = run_cli(checker, "create", backups, "--db", db,
                         "--parent", "3", "--json")
        created5 = json.loads(result.stdout)
        checker.check("grown backup id", created5["backup_id"], 5)

        aux = sqlite3.connect(db)
        aux.execute("DROP TABLE aux_bulk")
        aux.execute("VACUUM")
        aux.close()
        result = run_cli(checker, "create", backups, "--db", db,
                         "--parent", "5", "--json")
        created6 = json.loads(result.stdout)
        checker.check("shrunk backup id", created6["backup_id"], 6)
        checker.check_true("image obviously shrunk",
                           created6["image_size"]
                           < created5["image_size"] * 0.6)
        checker.check_true("shrunk image has fewer chunks",
                           created6["chunk_count"] < created5["chunk_count"])

        restored6 = os.path.join(workdir, "restored-6.sqlite")
        run_cli(checker, "restore", backups, "--id", "6",
                "--target", restored6)
        checker.check("shrunk restore: exact size (no stale tail)",
                      os.path.getsize(restored6), created6["image_size"])
        checker.check("shrunk restore: independent digest",
                      sha256_file(restored6), created6["image_sha256"])
        checker.check("shrunk restore: integrity",
                      raw_integrity_check(restored6), "ok")
        probe = sqlite3.connect(restored6)
        checker.check("shrunk restore: aux table gone",
                      probe.execute("SELECT COUNT(*) FROM sqlite_master"
                                    " WHERE name='aux_bulk'").fetchone()[0], 0)
        checker.check("shrunk restore: facts intact",
                      probe.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                      19)
        probe.close()
        checker.check("shrunk restore: business queries match production",
                      business_snapshot(restored6, dictionary), snap_stage2)

        # ---------------------------------------------------------- stage 4
        print("\n4. Re-backup identical content: new id, zero new chunks")
        files_before = chunk_file_count(backups)
        result = run_cli(checker, "create", backups, "--db", db,
                         "--parent", "6", "--json")
        created7 = json.loads(result.stdout)
        checker.check("re-backup id", created7["backup_id"], 7)
        checker.check("re-backup layer is empty", created7["layer_chunks"], 0)
        checker.check("re-backup wrote nothing",
                      created7["new_chunk_files"], 0)
        checker.check("chunk store unchanged", chunk_file_count(backups),
                      files_before)

        # ---------------------------------------------------------- stage 5
        print("\n5. Move production and original XBRL away; restore a"
              " 3-level chain offline")
        moved = os.path.join(workdir, "moved-away")
        os.rename(prod_dir, moved)
        checker.check_true("production really gone", not os.path.exists(db))
        checker.check_true("instances really gone",
                           not os.path.exists(os.path.join(
                               fixtures, "demo-A.xml")))

        restored3 = os.path.join(workdir, "restored-3.sqlite")
        run_cli(checker, "restore", backups, "--id", "3",
                "--target", restored3)
        m3 = manifest_of(backups, 3)
        checker.check("3-level restore: independent digest",
                      sha256_file(restored3), m3["image_sha256"])
        checker.check("3-level restore: integrity",
                      raw_integrity_check(restored3), "ok")
        checker.check("3-level restore: business snapshot matches",
                      business_snapshot(restored3, dictionary), snap_stage2)

        restored4 = os.path.join(workdir, "restored-4.sqlite")
        run_cli(checker, "restore", backups, "--id", "4",
                "--target", restored4)
        checker.check("branch restore: digest",
                      sha256_file(restored4), created4["image_sha256"])
        checker.check("branch restore: business snapshot matches",
                      business_snapshot(restored4, dictionary), snap_stage2)
        # The root-only state is also still recoverable.
        restored1 = os.path.join(workdir, "restored-1.sqlite")
        run_cli(checker, "restore", backups, "--id", "1",
                "--target", restored1)
        checker.check("root restore: stage0 business snapshot",
                      business_snapshot(restored1, dictionary), snap_stage0)

        # ---------------------------------------------------------- stage 6
        print("\n6. Prune preview/apply keeping the grandchild and the"
              " sibling branch")
        preview = run_cli(checker, "prune", backups, "--keep", "3",
                          "--keep", "4", "--json")
        plan = json.loads(preview.stdout)
        checker.check("preview: dependencies stay", plan["needed_dependencies"],
                      [1, 2])
        checker.check("preview: removable manifests",
                      plan["removable_manifests"], [5, 6, 7])
        checker.check_true("preview: chunks would be freed",
                           plan["removable_chunks"] > 0)
        checker.check("preview deleted nothing",
                      sorted(json.loads(run_cli(checker, "list", backups,
                                                "--json").stdout,
                                        )[i]["id"] for i in range(7)),
                      [1, 2, 3, 4, 5, 6, 7])
        applied = run_cli(checker, "prune", backups, "--keep", "3",
                          "--keep", "4", "--apply", "--json")
        applied_plan = json.loads(applied.stdout)
        checker.check("apply removed manifests",
                      applied_plan["removable_manifests"], [5, 6, 7])
        listing = json.loads(run_cli(checker, "list", backups,
                                     "--json").stdout)
        checker.check("remaining backups", [m["id"] for m in listing],
                      [1, 2, 3, 4])
        run_cli(checker, "check", backups)
        again3 = os.path.join(workdir, "again-3.sqlite")
        run_cli(checker, "restore", backups, "--id", "3", "--target", again3)
        checker.check("post-prune restore 3 digest", sha256_file(again3),
                      m3["image_sha256"])
        again4 = os.path.join(workdir, "again-4.sqlite")
        run_cli(checker, "restore", backups, "--id", "4", "--target", again4)
        checker.check("post-prune restore 4 digest", sha256_file(again4),
                      created4["image_sha256"])
        checker.check("post-prune business snapshot",
                      business_snapshot(again3, dictionary), snap_stage2)

        # ---------------------------------------------------------- stage 7
        print("\n7. WAL live writer: two markers advanced in one transaction")
        live_db = os.path.join(workdir, "live.sqlite")
        live_backups = os.path.join(workdir, "live-backups")
        conn = sqlite3.connect(live_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE marker_a (n INTEGER NOT NULL)")
        conn.execute("CREATE TABLE marker_b (n INTEGER NOT NULL)")
        conn.execute("CREATE TABLE churn (id INTEGER PRIMARY KEY"
                     " AUTOINCREMENT, pad TEXT)")
        conn.execute("INSERT INTO marker_a VALUES (0)")
        conn.execute("INSERT INTO marker_b VALUES (0)")
        conn.commit()
        conn.close()
        stop_file = os.path.join(workdir, "stop-writer")
        writer_code = (
            "import sqlite3, os, time\n"
            f"db, stop = {live_db!r}, {stop_file!r}\n"
            "conn = sqlite3.connect(db, isolation_level=None)\n"
            "while not os.path.exists(stop):\n"
            "    conn.execute('BEGIN IMMEDIATE')\n"
            "    conn.execute('UPDATE marker_a SET n = n + 1')\n"
            "    conn.execute('UPDATE marker_b SET n = n + 1')\n"
            "    conn.execute(\"INSERT INTO churn (pad) VALUES"
            " (hex(randomblob(512)))\")\n"
            "    conn.execute('COMMIT')\n"
            "    time.sleep(0.001)\n"
            "conn.close()\n"
        )
        writer = subprocess.Popen([sys.executable, "-I", "-c", writer_code])
        try:
            time.sleep(0.5)
            live_created = run_cli(checker, "create", live_backups,
                                   "--db", live_db, "--json")
        finally:
            open(stop_file, "w").close()
            writer.wait(timeout=30)
        checker.check("writer exited cleanly", writer.returncode, 0)
        live_id = json.loads(live_created.stdout)["backup_id"]
        live_out = os.path.join(workdir, "live-restored.sqlite")
        run_cli(checker, "restore", live_backups, "--id", str(live_id),
                "--target", live_out)
        probe = sqlite3.connect(live_out)
        marker_a, marker_b = probe.execute(
            "SELECT (SELECT n FROM marker_a), (SELECT n FROM marker_b)"
        ).fetchone()
        probe.close()
        checker.check_true("backup saw committed transactions", marker_a > 0)
        checker.check("restored markers equal (single committed state)",
                      marker_a, marker_b)
        probe = sqlite3.connect(live_db)
        final_a, final_b = probe.execute(
            "SELECT (SELECT n FROM marker_a), (SELECT n FROM marker_b)"
        ).fetchone()
        probe.close()
        checker.check("production kept writing after backup",
                      final_a == final_b and final_a >= marker_a, True)

        # ---------------------------------------------------------- stage 8
        print("\n8. Corrupted / missing shared chunk: restore fails,"
              " existing target untouched")
        corrupt_dir = os.path.join(workdir, "corrupt-backups")
        corrupt_db = os.path.join(workdir, "corrupt-src.sqlite")
        conn = sqlite3.connect(corrupt_db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)",
                         [(i, "q" * 500) for i in range(2000)])
        conn.commit()
        run_cli(checker, "create", corrupt_dir, "--db", corrupt_db)
        conn.execute("UPDATE t SET v='r' WHERE id=1")
        conn.commit()
        conn.close()
        run_cli(checker, "create", corrupt_dir, "--db", corrupt_db,
                "--parent", "1")
        # A chunk the child inherits from the root.
        show = run_cli(checker, "show", corrupt_dir, "--id", "2", "--json")
        spans = json.loads(show.stdout)["chunk_sources"]
        root_span = next(s for s in spans if s["layer"] == 1)
        m1c = manifest_of(corrupt_dir, 1)
        shared_digest = next(c["sha256"] for c in m1c["chunks"]
                             if c["index"] == root_span["first"])
        shared_path = os.path.join(
            corrupt_dir, "chunks", shared_digest[:2],
            f"{shared_digest}.chunk")
        with open(shared_path, "r+b") as handle:
            first = handle.read(1)
            handle.seek(0)
            handle.write(bytes([first[0] ^ 0xFF]))
        result = run_cli(checker, "check", corrupt_dir, expect=3)
        checker.check_true("check names corruption",
                           "corruption error" in result.stderr)
        target8 = os.path.join(workdir, "target-8.sqlite")
        with open(target8, "wb") as handle:
            handle.write(b"ORIGINAL-TARGET-BYTES")
        result = run_cli(checker, "restore", corrupt_dir, "--id", "2",
                         "--target", target8, "--replace", expect=3)
        checker.check_true("restore names corruption",
                           "corruption error" in result.stderr)
        with open(target8, "rb") as handle:
            checker.check("existing target bytes preserved",
                          handle.read(), b"ORIGINAL-TARGET-BYTES")
        fresh8 = os.path.join(workdir, "fresh-8.sqlite")
        run_cli(checker, "restore", corrupt_dir, "--id", "2",
                "--target", fresh8, expect=3)
        checker.check_true("no partial target left behind",
                           not os.path.exists(fresh8))
        # Missing chunk variant.
        os.unlink(shared_path)
        run_cli(checker, "restore", corrupt_dir, "--id", "2",
                "--target", os.path.join(workdir, "fresh-8b.sqlite"),
                expect=3)
        run_cli(checker, "check", corrupt_dir, expect=3)

        # ---------------------------------------------------------- stage 9
        print("\n9. Prune refuses missing parents, cycles, bad fields and"
              " out-of-range references")
        for label, mutate in (
            ("missing parent", lambda m: dict(m, id=7, parent=42)),
            ("cycle 7->8", None),  # handled specially below
            ("bad chunk_size", lambda m: dict(m, id=7, chunk_size=32768)),
            ("out-of-range chunk index", None),  # special below
        ):
            case_dir = os.path.join(workdir, f"refuse-{label[:5]}"
                                    f"-{len(label)}")
            run_cli(checker, "create", case_dir, "--db", corrupt_db)
            valid = manifest_of(case_dir, 1)
            if label == "cycle 7->8":
                a = dict(valid, id=7, parent=8)
                b = dict(valid, id=8, parent=7)
                for data in (a, b):
                    with open(os.path.join(case_dir, "manifests",
                                           f"{data['id']:06d}.json"),
                              "w") as handle:
                        json.dump(data, handle)
            elif label == "out-of-range chunk index":
                bad = dict(valid, id=7)
                entry = dict(valid["chunks"][0])
                entry["index"] = valid["chunk_count"]
                bad["chunks"] = [entry]
                with open(os.path.join(case_dir, "manifests",
                                       "000007.json"), "w") as handle:
                    json.dump(bad, handle)
            else:
                with open(os.path.join(case_dir, "manifests",
                                       "000007.json"), "w") as handle:
                    json.dump(mutate(valid), handle)
            manifests_before = sorted(os.listdir(os.path.join(
                case_dir, "manifests")))
            chunks_before = chunk_file_count(case_dir)
            run_cli(checker, "prune", case_dir, "--keep", "1", expect=3)
            run_cli(checker, "prune", case_dir, "--keep", "1", "--apply",
                    expect=3)
            checker.check(f"{label}: manifests untouched",
                          sorted(os.listdir(os.path.join(case_dir,
                                                         "manifests"))),
                          manifests_before)
            checker.check(f"{label}: chunks untouched",
                          chunk_file_count(case_dir), chunks_before)

        # --------------------------------------------------------- stage 10
        print("\n10. Lock coordination: prune cannot delete chunks a restore"
              " is reading")
        lock_dir = os.path.join(workdir, "lock-backups")
        lock_db = os.path.join(workdir, "lock-src.sqlite")
        conn = sqlite3.connect(lock_db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)",
                         [(i, "w" * 300) for i in range(3000)])
        conn.commit()
        run_cli(checker, "create", lock_dir, "--db", lock_db)
        conn.execute("UPDATE t SET v='u2' WHERE id BETWEEN 1 AND 900")
        conn.commit()
        run_cli(checker, "create", lock_dir, "--db", lock_db, "--parent", "1")
        conn.execute("UPDATE t SET v='u3' WHERE id BETWEEN 901 AND 1800")
        conn.commit()
        conn.close()
        run_cli(checker, "create", lock_dir, "--db", lock_db, "--parent", "2")
        m3l = manifest_of(lock_dir, 3)
        gate = os.path.join(workdir, "gate")
        out10 = os.path.join(workdir, "restored-10.sqlite")
        restore_proc = subprocess.Popen(
            [sys.executable, "-m", "warehouse_backup.cli", "restore",
             lock_dir, "--id", "3", "--target", out10,
             "--pause-gate", gate],
            cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        deadline = time.time() + 30
        while not os.path.exists(gate + ".acquired"):
            checker.check_true("restore acquired dependencies in time",
                               time.time() < deadline)
            time.sleep(0.05)
        prune_proc = subprocess.Popen(
            [sys.executable, "-m", "warehouse_backup.cli", "prune",
             lock_dir, "--keep", "1", "--apply"],
            cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        time.sleep(2.0)
        checker.check("prune blocked while restore holds dependencies",
                      prune_proc.poll(), None)
        checker.check_true("dependency manifests not deleted early",
                           os.path.exists(os.path.join(
                               lock_dir, "manifests", "000003.json")))
        open(gate + ".go", "w").close()
        r_out, r_err = restore_proc.communicate(timeout=60)
        p_out, p_err = prune_proc.communicate(timeout=60)
        checker.check("paused restore still succeeds",
                      restore_proc.returncode, 0)
        checker.check("restore read exactly the pinned image",
                      sha256_file(out10), m3l["image_sha256"])
        checker.check("prune eventually completes", prune_proc.returncode, 0)
        checker.check_true("prune deleted only after restore finished",
                           not os.path.exists(os.path.join(
                               lock_dir, "manifests", "000003.json")))

        # --------------------------------------------------------- stage 11
        print("\n11. Symlink substitution is refused")
        link_dir = os.path.join(workdir, "link-backups")
        run_cli(checker, "create", link_dir, "--db", corrupt_db)
        victim_digest = manifest_of(link_dir, 1)["chunks"][0]["sha256"]
        victim = os.path.join(link_dir, "chunks", victim_digest[:2],
                              f"{victim_digest}.chunk")
        real = os.path.join(workdir, "real-chunk.bin")
        os.rename(victim, real)
        os.symlink(real, victim)
        run_cli(checker, "check", link_dir, expect=3)
        run_cli(checker, "restore", link_dir, "--id", "1",
                "--target", os.path.join(workdir, "link-out.sqlite"),
                expect=3)
        os.unlink(victim)
        os.rename(real, victim)
        os.symlink(os.path.join("..", "..", "etc", "hostname"),
                   os.path.join(link_dir, "manifests", "000009.json"))
        run_cli(checker, "check", link_dir, expect=3)
        os.unlink(os.path.join(link_dir, "manifests", "000009.json"))
        run_cli(checker, "check", link_dir)

        # --------------------------------------------------------- stage 12
        print("\n12. Exit-code classes and the original commands")
        run_cli(checker, "restore", backups, "--id", "99",
                "--target", os.path.join(workdir, "x.sqlite"), expect=2)
        run_cli(checker, "restore", backups, "--id", "3",
                "--target", again3, expect=2)  # exists, no --replace
        run_cli(checker, "restore", backups, "--id", "3", "--target",
                os.path.join(workdir, "no-such-dir", "x.sqlite"), expect=4)
        run_cli(checker, "prune", backups, "--keep", "99", expect=2)
        run_cli(checker, "create", backups, "--db", db, expect=2)  # moved away

        query = subprocess.run(
            [sys.executable, "-m", "xbrl_ingest.cli", "query", again3,
             "--dict", dictionary, "--concept-local", "Revenue", "--json"],
            cwd=PROJECT, capture_output=True, text=True)
        checker.check("original query CLI on restored warehouse",
                      query.returncode, 0)
        statuses = sorted(r["status"] for r in json.loads(query.stdout))
        checker.check("restored Revenue statuses", statuses,
                      ["conflict", "single"])
        ingest_new = subprocess.run(
            [sys.executable, "-m", "xbrl_ingest.cli", "ingest", again3,
             "--dict", dictionary,
             os.path.join(moved, "instances", "demo-C.xml")],
            cwd=PROJECT, capture_output=True, text=True)
        checker.check("original ingest CLI on restored warehouse",
                      ingest_new.returncode, 0)
        checker.check_true("re-ingest recognised as already present",
                           "already_ingested" in ingest_new.stdout)
        suite = subprocess.run(
            [sys.executable, "-m", "unittest", "discover"],
            cwd=PROJECT, capture_output=True, text=True)
        checker.check("full unittest suite (ingest/export/calc/backup)",
                      suite.returncode, 0)
        if suite.returncode != 0:
            print(suite.stdout[-3000:])
            print(suite.stderr[-3000:])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return checker.finish()


if __name__ == "__main__":
    raise SystemExit(main())
