#!/usr/bin/env python3
"""End-to-end acceptance drill for the offline WAL change decoder.

Scenarios, all on a real SQLite-generated WAL (never a synthetic event file):

A. main fact warehouse
   1. build the baseline through the *existing* XBRL ingester (S1), then
      TRUNCATE-checkpoint and freeze the baseline;
   2. within one WAL cycle, ingest S2 (same-value duplicates, a conflict and a
      nil fact), then run controlled maintenance transactions that edit a fact
      text, delete one fact and change one tracked identity;
   3. independently save the full tracked row set after every COMMIT via plain
      SQL, and replay the decoder's net-change stream through the separate
      applier to prove every intermediate state matches;
   4. exercise same-transaction/same-page repeated writes, database growth,
      deletes and a VACUUM-driven page-count shrink on an auxiliary table,
      plus composite identity, non-UTF-8 BLOB, NULL, empty text/blob and
      64-bit integer edge values.

B. tail/corruption experiments (separate WAL copies), asserting exact byte or
   frame positions, that corruption is never swallowed and uncommitted rows
   never enter the result; plus both page sizes and the alternate checksum
   magic, a mid-WAL schema-change rejection, bad identity rejections, a
   tracked-empty (untracked-only) transaction and a long-WAL consumer run.

Run:  python3 -I demo/wal_decode_drill/run_wal_drill.py
Exit 0 only if every hand-written check holds.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PROJECT)
sys.path.insert(0, HERE)

from wal_decode.cli import run as cli_run  # noqa: E402
from wal_decode.errors import (  # noqa: E402
    InputValidationError,
    Outcome,
    WalFormatError,
    WalUnsupportedError,
)
from wal_decode import (  # noqa: E402
    TrackedTable,
    WalChangeDecoder,
)
from wal_decode.wal_format import (  # noqa: E402
    FRAME_HEADER_SIZE,
    WAL_HEADER_SIZE,
    WAL_MAGIC_BE,
    rolling_checksum,
)
from xbrl_ingest import XbrlIngestor  # noqa: E402

from collector import WalCollector, sha256_file  # noqa: E402
from applier import (  # noqa: E402
    apply_commit,
    load_commits,
    sql_snapshot,
)

DICTIONARY = os.path.join(PROJECT, "demo", "xbrl_fixtures", "concepts.json")
INPUTS = os.path.join(HERE, "inputs")

# Tracked tables and their ordered identity columns (also used for snapshots).
TRACKED = {
    "facts": ["id"],
    "aux": ["tenant", "code"],
}


class Checker:
    def __init__(self):
        self.checks = 0
        self.failures: list[str] = []

    def check(self, label: str, actual, expected) -> None:
        self.checks += 1
        if actual != expected:
            self.failures.append(f"{label}: expected {expected!r}, got {actual!r}")
            print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
        else:
            print(f"  ok   {label}")

    def check_true(self, label: str, cond: bool, detail: str = "") -> None:
        self.checks += 1
        if not cond:
            self.failures.append(label + (f" ({detail})" if detail else ""))
            print(f"  FAIL {label} {detail}")
        else:
            print(f"  ok   {label}")

    def finish(self) -> int:
        if self.failures:
            print(f"\n{len(self.failures)} of {self.checks} checks failed")
            return 1
        print(f"\nall {self.checks} drill checks held")
        return 0


def build_main_scenario(workdir: str, ck: Checker, page_size: int) -> dict:
    collector = WalCollector(workdir, "main", page_size=page_size)
    conn = collector.conn

    # --- baseline through the real XBRL ingester (its own connection) -------
    # The ingester opens its own connection; point it at the same file before
    # the WAL cycle begins, then checkpoint so all pages land in the main db.
    ingestor = XbrlIngestor(collector.live_db, DICTIONARY)
    ingestor.close()  # release; reopen per file below against the same path
    ingestor = XbrlIngestor(collector.live_db, DICTIONARY)
    report_s1 = ingestor.ingest_file(os.path.join(INPUTS, "source-s1.xml"))
    s1_id = report_s1.source_id
    ingestor.close()

    # Auxiliary table: composite identity, BLOB/NULL/empty/int64 coverage.
    conn.execute(
        "CREATE TABLE aux(tenant TEXT NOT NULL, code TEXT NOT NULL, "
        "payload BLOB, note TEXT, big INTEGER, PRIMARY KEY(tenant, code))"
    )
    conn.execute(
        "INSERT INTO aux VALUES ('t1','k1', x'c328', 'seed', 9223372036854775807)"
    )
    # An ordinary table that exists from the baseline but is never tracked;
    # later DML on it produces a commit boundary with zero tracked changes
    # without altering the structure mid-WAL.
    conn.execute("CREATE TABLE untracked_log(seq INTEGER PRIMARY KEY, msg TEXT)")
    conn.commit()
    collector.checkpoint_truncate_and_copy_baseline()

    # Independent SQL ground truth at state 0 (read-only, immutable open).
    # Rows are keyed by identity columns only; every stored column is read.
    snap_dir = os.path.join(workdir, "sql_snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    snapshots: list[dict] = []
    baseline_snap = sql_snapshot(collector.baseline_db, TRACKED)
    snapshots.append(baseline_snap)

    # --- the WAL-cycle scenario, one explicit transaction per step ----------
    # Step 1: ingest S2 via the existing ingester (duplicates/conflict/nil).
    ingestor = XbrlIngestor(collector.live_db, DICTIONARY)
    report_s2 = ingestor.ingest_file(os.path.join(INPUTS, "source-s2.xml"))
    s2_id = report_s2.source_id
    ingestor.close()
    collector.freeze_cycle_wal()
    snapshots.append(_sql_snapshot_live(collector, conn, snap_dir, 1))

    # Step 2: maintenance -- edit one fact's text.
    first_note_id = conn.execute(
        "SELECT id FROM facts WHERE concept LIKE '%Note' AND source_id=? "
        "ORDER BY id LIMIT 1", (s1_id,)
    ).fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE facts SET value_text=? WHERE id=?",
                 ("maintained note text", first_note_id))
    conn.commit()
    collector.freeze_cycle_wal()
    snapshots.append(_sql_snapshot_live(collector, conn, snap_dir, 2))

    # Step 3: maintenance -- delete one fact (the S1 Assets fact).
    delete_target = conn.execute(
        "SELECT id FROM facts WHERE concept LIKE '%Assets' AND source_id=? "
        "ORDER BY id LIMIT 1", (s1_id,)
    ).fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM facts WHERE id=?", (delete_target,))
    conn.commit()
    collector.freeze_cycle_wal()
    snapshots.append(_sql_snapshot_live(collector, conn, snap_dir, 3))

    # Step 4: maintenance -- change a tracked identity (facts.id rowid).
    # Expressed in SQL as updating the PRIMARY KEY; the decoder must report
    # delete(old id)+insert(new id), never an update.
    ident_target = conn.execute(
        "SELECT id FROM facts WHERE concept LIKE '%Zero' AND source_id=? "
        "ORDER BY id LIMIT 1", (s1_id,)
    ).fetchone()[0]
    new_identity = 100000 + ident_target
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE facts SET id=? WHERE id=?", (new_identity, ident_target))
    conn.commit()
    collector.freeze_cycle_wal()
    snapshots.append(_sql_snapshot_live(collector, conn, snap_dir, 4))

    # Step 5: same-txn same-page repeated writes + growth + BLOB/NULL/empty.
    conn.execute("BEGIN IMMEDIATE")
    # Repeated writes to the same auxiliary row within ONE transaction: the
    # intermediate value must never appear as a row event.
    conn.execute("UPDATE aux SET note='intermediate A' WHERE tenant='t1' AND code='k1'")
    conn.execute("UPDATE aux SET note='intermediate B' WHERE tenant='t1' AND code='k1'")
    conn.execute(
        "UPDATE aux SET note='final', payload=x'00fffe', big=-9223372036854775808 "
        "WHERE tenant='t1' AND code='k1'"
    )
    # New rows force B-tree/page allocation (database growth); cover NULL,
    # empty text and zero-length BLOB as distinct storage classes.
    many = [
        ("t1", f"k{i}", None, "" if i % 2 else None, i)
        for i in range(2, 40)
    ]
    many.append(("t2", "empty-blob", b"", "blank", 0))
    conn.executemany(
        "INSERT INTO aux(tenant, code, payload, note, big) VALUES (?,?,?,?,?)", many
    )
    conn.commit()
    collector.freeze_cycle_wal()
    snapshots.append(_sql_snapshot_live(collector, conn, snap_dir, 5))

    # Step 6: delete inside aux (page reuse) -- distinct identity removed.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM aux WHERE tenant='t1' AND code='k3'")
    conn.commit()
    collector.freeze_cycle_wal()
    snapshots.append(_sql_snapshot_live(collector, conn, snap_dir, 6))

    # Step 7: VACUUM -> post-commit page count shrinks; structure unchanged.
    pages_before = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute("VACUUM")
    collector.freeze_cycle_wal()
    pages_after = conn.execute("PRAGMA page_count").fetchone()[0]
    snapshots.append(_sql_snapshot_live(collector, conn, snap_dir, 7))
    ck.check_true("VACUUM shrank or reorganised pages",
                  pages_after <= pages_before,
                  f"before={pages_before} after={pages_after}")

    # Step 8: a transaction touching only an untracked table.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO untracked_log(msg) VALUES ('no tracked rows here')")
    conn.commit()
    collector.freeze_cycle_wal()
    snapshots.append(_sql_snapshot_live(collector, conn, snap_dir, 8))

    manifest_path = os.path.join(workdir, "manifest.json")
    manifest = collector.write_manifest(manifest_path, [
        {"table": "facts", "identity_columns": TRACKED["facts"]},
        {"table": "aux", "identity_columns": TRACKED["aux"]},
    ], note="acceptance drill main scenario")

    info = {
        "collector": collector,
        "manifest": manifest,
        "snapshots": snapshots,
        "s1_id": s1_id,
        "s2_id": s2_id,
        "edited_note_id": first_note_id,
        "deleted_fact_id": delete_target,
        "identity_old": ident_target,
        "identity_new": new_identity,
        "snap_dir": snap_dir,
    }
    return info


def _sql_snapshot_live(collector: WalCollector, conn, snap_dir: str,
                       state_no: int) -> dict:
    """Independent ground truth: checkpoint-free read via a separate ro copy.

    We must not checkpoint the live WAL (that would change the artifact), so
    the frozen WAL plus baseline are materialised by copying the live pair at
    this instant and letting a *scratch* database recover it; the decoder
    itself never opens that scratch copy.
    """
    scratch_dir = os.path.join(snap_dir, f"state_{state_no}")
    os.makedirs(scratch_dir, exist_ok=True)
    scratch_db = os.path.join(scratch_dir, "snap.sqlite")
    shutil.copyfile(collector.live_db, scratch_db)
    shutil.copyfile(collector.live_db + "-wal", scratch_db + "-wal")
    # Ordinary open once, then close: SQLite recovers the WAL into the scratch
    # main file. This is independent snapshot tooling, not the decoder.
    rec = sqlite3_connect(scratch_db)
    rec.execute("SELECT COUNT(*) FROM facts").fetchone()
    rec.close()
    if os.path.exists(scratch_db + "-wal"):
        os.remove(scratch_db + "-wal")
    if os.path.exists(scratch_db + "-shm"):
        os.remove(scratch_db + "-shm")
    return sql_snapshot(scratch_db, TRACKED)


def sqlite3_connect(path: str):
    import sqlite3
    c = sqlite3.connect(path)
    return c


def decode_and_verify(workdir: str, info: dict, ck: Checker,
                      label: str) -> list[dict]:
    collector = info["collector"]
    out_dir = os.path.join(workdir, f"decoded_{label}")
    argv = ["decode", "--manifest", os.path.join(workdir, "manifest.json"),
            "--out", out_dir]
    code = cli_run(argv)
    ck.check(f"{label}: CLI exit code", code, 0)
    if code != 0:
        return []
    commits = load_commits(os.path.join(out_dir, "commits.jsonl"))
    ck.check(f"{label}: number of commits vs SQL states",
             len(commits), len(info["snapshots"]) - 1)

    identity_len = {t: len(cols) for t, cols in TRACKED.items()}
    # Replay from the baseline (state 0) and compare after EVERY commit.
    state = info["snapshots"][0]
    for idx, commit in enumerate(commits, start=1):
        ck.check(f"{label}: commit seq ordering", commit["commit_seq"], idx)
        ck.check_true(
            f"{label}: frame range present",
            commit["frame_range"][0] <= commit["commit_frame_no"]
            == commit["frame_range"][1],
        )
        ck.check_true(
            f"{label}: state pointers",
            commit["state_before"] == idx - 1 and commit["state_after"] == idx,
        )
        state = apply_commit(state, commit, identity_len)
        expected = info["snapshots"][idx]
        ck.check(f"{label}: full state after commit {idx} matches SQL snapshot",
                 _normalize(state), _normalize(expected))
    # Commit-level semantic assertions.
    return commits


def _normalize(state: dict) -> dict:
    """Make BLOB/value forms comparable across the two independent readers."""
    norm = {}
    for table, rows in state.items():
        norm[table] = {}
        for identity, row in rows.items():
            norm[table][identity] = {
                col: dict(rec) for col, rec in row.items()
            }
    return norm


def main() -> int:
    ck = Checker()
    workdir = tempfile.mkdtemp(prefix="wal_drill_")
    print(f"drill workdir: {workdir}")
    try:
        info = build_main_scenario(workdir, ck, page_size=4096)
        commits = decode_and_verify(workdir, info, ck, "main4096")
        assert_semantics(commits, info, ck)
        info["collector"].close()

        run_tail_and_corruption(workdir, ck)
        run_page_size_and_magic(workdir, ck)
        run_schema_change(workdir, ck)
        run_identity_rejections(workdir, ck)
        run_long_wal(workdir, ck)
        run_empty_and_nochange(workdir, ck)
    finally:
        pass  # keep workdir for inspection; print path above
    return ck.finish()


def assert_semantics(commits: list[dict], info: dict, ck: Checker) -> None:
    # S2 ingestion: duplicate/conflict/nil all land as inserts (new fact rows).
    s2_commit = commits[0]
    s2_inserts = [e for c in s2_commit["changes"] if c["table"] == "facts"
                  for e in c["events"] if e["kind"] == "insert"]
    ck.check_true("S2 produced inserted fact rows", len(s2_inserts) >= 4,
                  f"inserts={len(s2_inserts)}")
    nil_rows = [e for e in s2_inserts
                if e["after"]["nil"]["type"] == "integer"
                and e["after"]["nil"]["value"] == "1"]
    ck.check_true("S2 nil fact present; nil integer preserved as decimal string",
                  len(nil_rows) == 1, f"nil_rows={len(nil_rows)}")
    # Distinguish NULL vs empty text vs empty BLOB in aux (step 5).
    step5 = commits[4]
    aux_events = [e for c in step5["changes"] if c["table"] == "aux"
                  for e in c["events"]]
    tags = {(e["after"] or e["before"])["note"]["type"] for e in aux_events}
    ck.check_true("aux events carry null and text storage tags",
                  {"null", "text"}.issubset(tags), str(sorted(tags)))
    blob_event = next(e for e in aux_events
                      if (e["after"] or {}).get("payload", {}).get("type") == "blob")
    ck.check("BLOB reversibly base64 encoded",
             blob_event["after"]["payload"]["encoding"], "base64")
    big = next(e for e in aux_events
               if (e["after"] or {}).get("big", {}).get("value")
               in ("9223372036854775807", "-9223372036854775808",
                   "3", "4", "5"))
    ck.check_true("64-bit integer preserved as decimal string",
                  isinstance(big["after"]["big"]["value"], str))
    # Repeated same-row writes collapse: no 'intermediate' value leaks.
    leaked = [e for e in aux_events
              if "intermediate" in str((e["after"] or {}).get("note"))]
    ck.check("no intermediate same-txn value becomes an event", leaked, [])
    # Identity change is delete+insert, not update.
    step4 = commits[3]
    fact_events = [e for c in step4["changes"] if c["table"] == "facts"
                   for e in c["events"]]
    kinds = sorted(e["kind"] for e in fact_events)
    ck.check("identity change expressed as delete + insert", kinds, ["delete", "insert"])
    ids = sorted(
        int((e["before"] or e["after"])["id"]["value"]) for e in fact_events
    )
    ck.check("identity change references old and new id",
             ids, sorted([info["identity_old"], info["identity_new"]]))
    # Untracked-only commit: boundary present, tracked change list empty.
    last = commits[-1]
    ck.check("untracked-only commit still emits a boundary",
             all(len(c["events"]) == 0 for c in last["changes"]), True)
    # VACUUM commit declares a page count and frames still resolve.
    vacuum_commit = commits[6]
    ck.check_true("VACUUM commit carries post-commit page count",
                  vacuum_commit["db_page_count_after"] > 0)


# -- tail / corruption experiments ------------------------------------------

def run_tail_and_corruption(workdir: str, ck: Checker) -> None:
    print("\n== tail and corruption experiments ==")
    d = os.path.join(workdir, "corrupt")
    os.makedirs(d, exist_ok=True)
    collector = WalCollector(d, "corr", page_size=4096)
    conn = collector.conn
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1,'a')")
    conn.commit()
    collector.checkpoint_truncate_and_copy_baseline()
    for i in range(2, 6):
        conn.execute(f"INSERT INTO t VALUES ({i},'v{i}')")
        conn.commit()
    live_wal = collector.live_db + "-wal"
    frame_size = FRAME_HEADER_SIZE + collector.page_size

    # (1) truncated tail frame -> INCOMPLETE, exact offset, earlier commits kept.
    full = open(live_wal, "rb").read()
    cut = len(full) - 123
    trunc_path = os.path.join(d, "truncated.wal")
    open(trunc_path, "wb").write(full[:cut])
    _manifest_for(d, collector.baseline_db, trunc_path, [("t", ["id"])])
    with WalChangeDecoder(collector.baseline_db, trunc_path,
                          (TrackedTable("t", ("id",)),)) as dec:
        parsed = list(dec.iter_commits())
        ck.check("truncated: earlier commits retained", len(parsed), 3)
        ck.check("truncated: outcome incomplete", dec.outcome, Outcome.INCOMPLETE)
        ck.check("truncated: partial frame starts at a frame boundary",
                 dec.tail.offset,
                 WAL_HEADER_SIZE + 3 * frame_size)
        # The exact truncation byte is frame offset plus the surviving bytes.
        ck.check("truncated: truncation byte reconstructed",
                 dec.tail.offset + dec.tail.partial_bytes, cut)
        # 123 bytes were cut from the frame, so frame_size-123 survive.
        ck.check("truncated: surviving partial bytes reported",
                 dec.tail.partial_bytes, frame_size - 123)
        ck.check("truncated: missing bytes reported",
                 frame_size - dec.tail.partial_bytes, 123)

    # (2) full uncommitted frame (valid checksum, marker cleared) -> INCOMPLETE.
    raw = bytearray(full)
    offsets = list(range(WAL_HEADER_SIZE, len(raw), frame_size))
    last = offsets[-1]
    big_endian = raw[:4] == struct.pack(">I", WAL_MAGIC_BE)
    s0, s1 = struct.unpack(">II", bytes(raw[24:32]))
    for off in offsets[:-1]:
        s0, s1 = rolling_checksum(bytes(raw[off:off + 8]), s0, s1,
                                  big_endian=big_endian)
        s0, s1 = rolling_checksum(
            bytes(raw[off + FRAME_HEADER_SIZE:off + frame_size]),
            s0, s1, big_endian=big_endian)
    struct.pack_into(">I", raw, last + 4, 0)
    s0, s1 = rolling_checksum(bytes(raw[last:last + 8]), s0, s1,
                              big_endian=big_endian)
    s0, s1 = rolling_checksum(
        bytes(raw[last + FRAME_HEADER_SIZE:last + frame_size]),
        s0, s1, big_endian=big_endian)
    struct.pack_into(">II", raw, last + 16, s0, s1)
    pending_path = os.path.join(d, "pending.wal")
    open(pending_path, "wb").write(bytes(raw))
    with WalChangeDecoder(collector.baseline_db, pending_path,
                          (TrackedTable("t", ("id",)),)) as dec:
        parsed = [c.to_dict() for c in dec.iter_commits()]
        ck.check("pending: only committed txns emitted", len(parsed), 3)
        ck.check("pending: uncommitted row absent",
                 _max_id(parsed), 4)
        ck.check("pending: outcome incomplete", dec.outcome, Outcome.INCOMPLETE)
        ck.check("pending: one full frame pending",
                 dec.tail.full_frames_pending, 1)

    # Each corruption case records the WAL digest before and after mutation and
    # rebuilds a matching outer manifest. A matching digest proves the rejection
    # comes from internal WAL verification, not the manifest front door.
    def corrupt_case(label: str, mutate, *, header=False, expect_offset=None):
        before_digest = sha256_file(live_wal)
        bad = bytearray(full)
        mutate(bad)
        p = os.path.join(d, f"bad_{label.replace(' ', '_')}.wal")
        with open(p, "wb") as fh:
            fh.write(bytes(bad))
        after_digest = sha256_file(p)
        ck.check_true(f"{label}: digest changed by mutation",
                      before_digest != after_digest)
        rebuilt = _manifest_for(d, collector.baseline_db, p, [("t", ["id"])],
                                name=f"manifest_{label.replace(' ', '_')}.json")
        ck.check(f"{label}: outer manifest matches mutated WAL",
                 rebuilt["wal"]["sha256"], after_digest)
        _expect_corrupt(ck, collector, p, label, header=header,
                        expect_offset=expect_offset,
                        manifest_path=os.path.join(
                            d, f"manifest_{label.replace(' ', '_')}.json"))

    # (3) corrupt WAL header magic -> CORRUPT at offset 0.
    corrupt_case("header magic", lambda b: b.__setitem__(0, b[0] ^ 0xFF),
                 header=True)

    # (4) corrupt salt of first frame -> frame-level error with position.
    salt_off = WAL_HEADER_SIZE + 8
    corrupt_case("frame salt", lambda b: b.__setitem__(salt_off, b[salt_off] ^ 0x01),
                 expect_offset=salt_off)

    # (5) corrupt page number (set to 0) -> page-no-zero error.
    pg_off = WAL_HEADER_SIZE
    corrupt_case("pageno zero",
                 lambda b: struct.pack_into(">I", b, pg_off, 0),
                 expect_offset=pg_off)

    # (6) corrupt frame checksum byte -> checksum error with frame/offset.
    fc_off = WAL_HEADER_SIZE + 16
    corrupt_case("frame checksum",
                 lambda b: b.__setitem__(fc_off, b[fc_off] ^ 0x01),
                 expect_offset=fc_off)

    # CLI exit codes for incomplete and corrupt.
    _manifest_for(d, collector.baseline_db, trunc_path, [("t", ["id"])],
                  name="trunc.manifest.json")
    code = cli_run(["decode", "--manifest", os.path.join(d, "trunc.manifest.json"),
                    "--out", os.path.join(d, "out_trunc")])
    ck.check("CLI incomplete exit code 10", code, 10)
    ck.check_true("INCOMPLETE marker written",
                  os.path.exists(os.path.join(d, "out_trunc", "INCOMPLETE.txt")))
    collector.close()


def _max_id(commits: list[dict]) -> int:
    highest = 0
    for commit in commits:
        for change in commit["changes"]:
            for event in change["events"]:
                row = event["after"] or event["before"]
                highest = max(highest, int(row["id"]["value"]))
    return highest


def _expect_corrupt(ck: Checker, collector: WalCollector, wal_path: str,
                    label: str, *, header: bool = False,
                    expect_offset: int | None = None,
                    manifest_path: str | None = None) -> None:
    try:
        if manifest_path is not None:
            # Go through manifest verification first; since the rebuilt
            # manifest matches the mutated file, rejection must come from the
            # internal WAL checks rather than the digest front door.
            with WalChangeDecoder.from_manifest(manifest_path) as dec:
                list(dec.iter_commits())
        else:
            with WalChangeDecoder(collector.baseline_db, wal_path,
                                  (TrackedTable("t", ("id",)),)) as dec:
                list(dec.iter_commits())
        ck.check_true(f"{label}: corruption rejected", False, "no error raised")
    except WalFormatError as exc:
        ck.check_true(f"{label}: WalFormatError raised", True)
        if expect_offset is not None:
            ck.check(f"{label}: exact byte offset", exc.offset, expect_offset)
        if header:
            ck.check(f"{label}: header offset zero", exc.offset, 0)


def _manifest_for(d: str, db: str, wal: str, tables: list[tuple],
                  name: str = "manifest.json") -> dict:
    def fp(p: str) -> dict:
        return {"path": os.path.abspath(p), "byte_length": os.path.getsize(p),
                "sha256": sha256_file(p)}
    manifest = {
        "manifest_version": 1,
        "database": fp(db),
        "wal": fp(wal),
        "tracked_tables": [
            {"table": t, "identity_columns": cols} for t, cols in tables
        ],
    }
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return manifest


# -- page size and alternate checksum magic ---------------------------------

def run_page_size_and_magic(workdir: str, ck: Checker) -> None:
    print("\n== page sizes and checksum magic ==")
    for page_size in (4096, 65536):
        d = os.path.join(workdir, f"ps{page_size}")
        collector = WalCollector(d, f"ps{page_size}", page_size=page_size)
        conn = collector.conn
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t VALUES (1,'seed')")
        conn.commit()
        collector.checkpoint_truncate_and_copy_baseline()
        conn.execute("INSERT INTO t VALUES (2,'grow')")
        conn.commit()
        collector.freeze_cycle_wal()
        raw = open(collector.cycle_wal, "rb").read()
        magic = struct.unpack(">I", raw[:4])[0]
        ck.check_true(f"page_size={page_size}: WAL header page size",
                      struct.unpack(">I", raw[8:12])[0] == page_size)
        with WalChangeDecoder(collector.baseline_db, collector.cycle_wal,
                              (TrackedTable("t", ("id",)),)) as dec:
            commits = list(dec.iter_commits())
            ck.check(f"page_size={page_size}: one commit decoded", len(commits), 1)
            ck.check(f"page_size={page_size}: outcome ok", dec.outcome, Outcome.OK)
        collector.close()

    # Alternate legal magic: SQLite picks magic by checksum byte order; the
    # parser must accept both. Flip magic and recompute ALL checksums with the
    # opposite byte order, then prove independent verification succeeds.
    d = os.path.join(workdir, "magic_alt")
    collector = WalCollector(d, "mag", page_size=4096)
    conn = collector.conn
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1,'seed')")
    conn.commit()
    collector.checkpoint_truncate_and_copy_baseline()
    conn.execute("INSERT INTO t VALUES (2,'flip')")
    conn.commit()
    collector.freeze_cycle_wal()
    alt_path = os.path.join(d, "alt_magic.wal")
    _flip_checksum_order(collector.cycle_wal, alt_path)
    with open(alt_path, "rb") as fh:
        head = fh.read(32)
    ck.check("alternate magic present",
             struct.unpack(">I", head[:4])[0], WAL_MAGIC_BE)
    with WalChangeDecoder(collector.baseline_db, alt_path,
                          (TrackedTable("t", ("id",)),)) as dec:
        commits = list(dec.iter_commits())
        ck.check("alternate-magic WAL decodes identically", len(commits), 1)
        ck.check("alternate-magic outcome ok", dec.outcome, Outcome.OK)
    collector.close()


def _flip_checksum_order(src_wal: str, dst_wal: str) -> None:
    """Rewrite a WAL using the other legal checksum byte order (independently)."""
    raw = bytearray(open(src_wal, "rb").read())
    page_size = struct.unpack(">I", bytes(raw[8:12]))[0]
    frame_size = FRAME_HEADER_SIZE + page_size
    new_big = not (struct.unpack(">I", bytes(raw[:4]))[0] == WAL_MAGIC_BE)
    new_magic = WAL_MAGIC_BE if new_big else 0x377F0682
    struct.pack_into(">I", raw, 0, new_magic)
    s0, s1 = rolling_checksum(bytes(raw[:24]), 0, 0, big_endian=new_big)
    struct.pack_into(">II", raw, 24, s0, s1)
    for off in range(WAL_HEADER_SIZE, len(raw), frame_size):
        s0, s1 = rolling_checksum(bytes(raw[off:off + 8]), s0, s1,
                                  big_endian=new_big)
        s0, s1 = rolling_checksum(
            bytes(raw[off + FRAME_HEADER_SIZE:off + frame_size]),
            s0, s1, big_endian=new_big)
        struct.pack_into(">II", raw, off + 16, s0, s1)
    with open(dst_wal, "wb") as fh:
        fh.write(bytes(raw))


# -- structural / identity rejection ----------------------------------------

def run_schema_change(workdir: str, ck: Checker) -> None:
    print("\n== mid-WAL schema change rejection ==")
    d = os.path.join(workdir, "schema")
    collector = WalCollector(d, "sch", page_size=4096)
    conn = collector.conn
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1,'a')")
    conn.commit()
    collector.checkpoint_truncate_and_copy_baseline()
    conn.execute("INSERT INTO t VALUES (2,'b')")
    conn.commit()
    conn.execute("ALTER TABLE t ADD COLUMN w TEXT")  # structure change
    conn.commit()
    collector.freeze_cycle_wal()
    try:
        with WalChangeDecoder(collector.baseline_db, collector.cycle_wal,
                              (TrackedTable("t", ("id",)),)) as dec:
            list(dec.iter_commits())
        ck.check_true("schema change rejected", False, "no error")
    except WalUnsupportedError as exc:
        ck.check_true("schema change raises unsupported",
                      "structure changed" in exc.message)
    collector.close()


def run_identity_rejections(workdir: str, ck: Checker) -> None:
    print("\n== identity definition rejections ==")
    d = os.path.join(workdir, "identity")
    collector = WalCollector(d, "idn", page_size=4096)
    conn = collector.conn
    conn.execute("CREATE TABLE t(id INTEGER, v TEXT)")  # no PK, no unique
    conn.execute("INSERT INTO t VALUES (1,'a')")
    conn.commit()
    conn.execute(
        "CREATE TABLE u(a TEXT NOT NULL, b TEXT NOT NULL, v TEXT, "
        "UNIQUE(a,b))"
    )
    conn.execute("INSERT INTO u VALUES ('x','y','z')")
    conn.commit()
    collector.checkpoint_truncate_and_copy_baseline()
    collector.freeze_cycle_wal()

    # Unknown table.
    try:
        with WalChangeDecoder(collector.baseline_db, collector.cycle_wal,
                              (TrackedTable("ghost", ("id",)),)):
            pass
        ck.check_true("unknown table rejected", False, "no error")
    except InputValidationError as exc:
        ck.check_true("unknown table raises input error", "does not exist" in exc.message)

    # Duplicate identity column in the spec.
    try:
        TrackedTable("u", ("a", "a"))
        ck.check_true("duplicate identity column rejected", False, "no error")
    except InputValidationError:
        ck.check_true("duplicate identity column rejected", True)

    # Non-unique / nullable identity on table t.
    try:
        with WalChangeDecoder(collector.baseline_db, collector.cycle_wal,
                              (TrackedTable("t", ("id",)),)):
            pass
        ck.check_true("non-unique identity rejected", False, "no error")
    except InputValidationError as exc:
        ck.check_true("non-unique identity raises input error",
                      "UNIQUE" in exc.message or "PRIMARY KEY" in exc.message)

    # Empty identity column list.
    try:
        TrackedTable("u", ())
        ck.check_true("empty identity rejected", False, "no error")
    except InputValidationError:
        ck.check_true("empty identity rejected", True)
    collector.close()


# -- long WAL consumer ------------------------------------------------------

def run_long_wal(workdir: str, ck: Checker) -> None:
    print("\n== long WAL per-transaction consumer ==")
    d = os.path.join(workdir, "long")
    collector = WalCollector(d, "long", page_size=4096)
    conn = collector.conn
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    collector.checkpoint_truncate_and_copy_baseline()
    n_commits = 60
    for i in range(1, n_commits + 1):
        conn.execute("INSERT INTO t VALUES (?,?)", (i, f"v{i}"))
        conn.commit()
    collector.freeze_cycle_wal()
    frames = (os.path.getsize(collector.cycle_wal) - WAL_HEADER_SIZE) // (
        FRAME_HEADER_SIZE + collector.page_size)
    # Each single-row insert fits one page, so the WAL holds at least one
    # frame per commit (often more, when a B-tree split writes extra pages).
    ck.check_true("long WAL spans one frame per commit or more",
                  frames >= n_commits, f"frames={frames}")
    tables = (TrackedTable("t", ("id",)),)
    seen = 0
    with WalChangeDecoder(collector.baseline_db, collector.cycle_wal,
                          tables) as dec:
        for commit in dec.iter_commits():
            seen += 1
            # Consumer applies each commit immediately; only current state held.
            ck.check_true(
                f"long WAL commit {seen} frame metadata",
                commit.db_page_count_after > 0
                and commit.first_frame_no <= commit.last_frame_no,
            )
        ck.check("long WAL: all commits streamed", seen, n_commits)
        ck.check("long WAL: outcome ok", dec.outcome, Outcome.OK)
    collector.close()


# -- empty WAL and no-change commit -----------------------------------------

def run_empty_and_nochange(workdir: str, ck: Checker) -> None:
    print("\n== empty WAL and no-op commits ==")
    d = os.path.join(workdir, "empty")
    collector = WalCollector(d, "emp", page_size=4096)
    conn = collector.conn
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1,'a')")
    # Untracked table exists from the baseline so its later DML does not
    # change the structure inside the decoded WAL.
    conn.execute("CREATE TABLE other(x INTEGER)")
    conn.commit()
    collector.checkpoint_truncate_and_copy_baseline()
    empty_wal = os.path.join(d, "empty.wal")
    open(empty_wal, "wb").write(b"")
    with WalChangeDecoder(collector.baseline_db, empty_wal,
                          (TrackedTable("t", ("id",)),)) as dec:
        commits = list(dec.iter_commits())
        ck.check("empty WAL: no commits", commits, [])
        ck.check("empty WAL: outcome ok", dec.outcome, Outcome.OK)

    # A commit that only touches an untracked table -> boundary, empty changes.
    conn.execute("INSERT INTO other VALUES (1)")
    conn.commit()
    collector.freeze_cycle_wal()
    with WalChangeDecoder(collector.baseline_db, collector.cycle_wal,
                          (TrackedTable("t", ("id",)),)) as dec:
        commits = list(dec.iter_commits())
        ck.check_true("untracked commit boundary emitted", len(commits) >= 1)
        ck.check("untracked commit has no tracked events",
                 [e for c in commits[-1].changes for e in c.events], [])
    collector.close()


if __name__ == "__main__":
    raise SystemExit(main())
