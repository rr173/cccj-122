"""Unit tests for transaction-level decoding semantics on real SQLite WALs."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from wal_decode import (
    Outcome,
    TrackedTable,
    WalChangeDecoder,
)
from wal_decode.errors import (
    InputValidationError,
    WalFormatError,
    WalUnsupportedError,
)


class WalHarness:
    def __init__(self, page_size=4096):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.sqlite")
        self.page_size = page_size
        self.conn = sqlite3.connect(self.db, isolation_level=None)
        self.conn.execute(f"PRAGMA page_size={page_size}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")

    def baseline(self):
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.base = os.path.join(self.tmp, "base.sqlite")
        shutil.copyfile(self.db, self.base)
        return self.base

    def wal_copy(self, name="cycle.wal"):
        path = os.path.join(self.tmp, name)
        shutil.copyfile(self.db + "-wal", path)
        return path

    def close(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def events_of(commit):
    return [(e.kind, (e.before or e.after))
            for change in commit.changes for e in change.events]


def id_of(event):
    row = event.after or event.before
    return int(row["id"]["value"])


class DecodeSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.h = WalHarness()

    def tearDown(self):
        self.h.close()

    def test_insert_update_delete_and_net_change(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        h.conn.execute("INSERT INTO t VALUES (1,'a')")
        h.conn.commit()
        base = h.baseline()
        h.conn.execute("INSERT INTO t VALUES (2,'b')")
        h.conn.commit()
        h.conn.execute("UPDATE t SET v='b2' WHERE id=2")
        h.conn.commit()
        h.conn.execute("DELETE FROM t WHERE id=1")
        h.conn.commit()
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("id",)),)) as dec:
            commits = list(dec.iter_commits())
            self.assertEqual(dec.outcome, Outcome.OK)
        self.assertEqual(len(commits), 3)
        c1, c2, c3 = commits
        self.assertEqual([(e.kind, id_of(e))
                          for ch in c1.changes for e in ch.events],
                         [("insert", 2)])
        self.assertEqual([(e.kind, id_of(e))
                          for ch in c2.changes for e in ch.events],
                         [("update", 2)])
        update = [e for ch in c2.changes for e in ch.events][0]
        self.assertEqual(update.before["v"]["value"], "b")
        self.assertEqual(update.after["v"]["value"], "b2")
        self.assertEqual([(e.kind, id_of(e))
                          for ch in c3.changes for e in ch.events],
                         [("delete", 1)])

    def test_same_txn_same_row_repeated_writes_collapse(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        h.conn.execute("INSERT INTO t VALUES (1,'a')")
        h.conn.commit()
        base = h.baseline()
        h.conn.execute("BEGIN IMMEDIATE")
        for v in ("x1", "x2", "x3"):
            h.conn.execute("UPDATE t SET v=? WHERE id=1", (v,))
        h.conn.commit()
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("id",)),)) as dec:
            commits = list(dec.iter_commits())
        events = [e for ch in commits[0].changes for e in ch.events]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "update")
        self.assertEqual(events[0].before["v"]["value"], "a")
        self.assertEqual(events[0].after["v"]["value"], "x3")

    def test_insert_then_delete_same_txn_leaves_no_event(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        h.conn.commit()
        base = h.baseline()
        h.conn.execute("BEGIN IMMEDIATE")
        h.conn.execute("INSERT INTO t VALUES (7,'tmp')")
        h.conn.execute("DELETE FROM t WHERE id=7")
        h.conn.commit()
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("id",)),)) as dec:
            commits = list(dec.iter_commits())
        self.assertEqual(len(commits), 1)  # boundary still emitted
        self.assertEqual(
            [e for ch in commits[0].changes for e in ch.events], [])

    def test_update_then_revert_same_txn_leaves_no_event(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        h.conn.execute("INSERT INTO t VALUES (1,'a')")
        h.conn.commit()
        base = h.baseline()
        h.conn.execute("BEGIN IMMEDIATE")
        h.conn.execute("UPDATE t SET v='changed' WHERE id=1")
        h.conn.execute("UPDATE t SET v='a' WHERE id=1")
        h.conn.commit()
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("id",)),)) as dec:
            commits = list(dec.iter_commits())
        self.assertEqual(
            [e for ch in commits[0].changes for e in ch.events], [])

    def test_identity_change_is_delete_plus_insert(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        h.conn.execute("INSERT INTO t VALUES (1,'a')")
        h.conn.commit()
        base = h.baseline()
        h.conn.execute("UPDATE t SET id=99 WHERE id=1")
        h.conn.commit()
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("id",)),)) as dec:
            commits = list(dec.iter_commits())
        kinds = sorted(
            (e.kind, id_of(e))
            for ch in commits[0].changes for e in ch.events)
        self.assertEqual(kinds, [("delete", 1), ("insert", 99)])

    def test_storage_classes_are_distinguished(self):
        h = self.h
        h.conn.execute(
            "CREATE TABLE t(k TEXT PRIMARY KEY, n INTEGER, r REAL, s TEXT, b BLOB)")
        h.conn.execute("INSERT INTO t(k) VALUES ('seed')")
        h.conn.commit()
        base = h.baseline()
        h.conn.execute(
            "INSERT INTO t VALUES ('row', 9223372036854775807, 2.5, '', x'')")
        h.conn.commit()
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("k",)),)) as dec:
            commits = list(dec.iter_commits())
        event = [e for ch in commits[0].changes for e in ch.events][0]
        after = event.after
        self.assertEqual(after["n"], {"type": "integer",
                                      "value": "9223372036854775807"})
        self.assertEqual(after["r"]["type"], "real")
        self.assertEqual(after["s"], {"type": "text", "value": ""})
        self.assertEqual(after["b"], {"type": "blob", "value": "",
                                      "encoding": "base64"})

    def test_null_vs_empty_text_vs_empty_blob_on_baseline(self):
        h = self.h
        h.conn.execute(
            "CREATE TABLE t(k INTEGER PRIMARY KEY, a TEXT, b BLOB)")
        h.conn.execute("INSERT INTO t VALUES (1, NULL, NULL)")
        h.conn.execute("INSERT INTO t VALUES (2, '', x'')")
        h.conn.commit()
        base = h.baseline()
        h.conn.execute("INSERT INTO t VALUES (3, 'x', x'01')")
        h.conn.commit()
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("k",)),)) as dec:
            # Re-read the baseline through the decoder's own snapshot path.
            baseline_snap = dec._previous["t"]
        self.assertEqual(baseline_snap[(1,)]["a"], {"type": "null", "value": None})
        self.assertEqual(baseline_snap[(2,)]["a"], {"type": "text", "value": ""})
        self.assertEqual(baseline_snap[(2,)]["b"]["type"], "blob")
        self.assertEqual(baseline_snap[(2,)]["b"]["value"], "")

    def test_vacuum_truncates_image_and_still_decodes(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        for i in range(5000):
            h.conn.execute("INSERT INTO t VALUES (?,?)", (i, f"v{i}" * 4))
        h.conn.commit()
        h.conn.execute("DELETE FROM t WHERE id >= 100")
        h.conn.commit()
        base = h.baseline()
        before_vacuum = h.conn.execute("PRAGMA page_count").fetchone()[0]
        h.conn.execute("VACUUM")
        after_vacuum = h.conn.execute("PRAGMA page_count").fetchone()[0]
        self.assertLess(after_vacuum, before_vacuum)
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("id",)),)) as dec:
            commits = list(dec.iter_commits())
            self.assertEqual(dec.outcome, Outcome.OK)
        # The VACUUM commit declares the smaller post-commit page count.
        self.assertEqual(commits[-1].db_page_count_after, after_vacuum)

    def test_growth_tracks_declared_page_count(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        h.conn.commit()
        base = h.baseline()
        h.conn.executemany(
            "INSERT INTO t(v) VALUES (?)",
            [(f"padding-{i}",) for i in range(2000)],
        )
        h.conn.commit()
        declared = h.conn.execute("PRAGMA page_count").fetchone()[0]
        wal = h.wal_copy()
        with WalChangeDecoder(base, wal, (TrackedTable("t", ("id",)),)) as dec:
            commits = list(dec.iter_commits())
        self.assertEqual(commits[-1].db_page_count_after, declared)

    def test_composite_identity_uses_tuple_not_string(self):
        h = self.h
        h.conn.execute(
            "CREATE TABLE t(a TEXT NOT NULL, b TEXT NOT NULL, v TEXT, "
            "PRIMARY KEY(a,b))")
        h.conn.execute("INSERT INTO t VALUES ('x','1','old')")
        h.conn.commit()
        base = h.baseline()
        # ('ab','c') and ('a','bc') share the naive concatenation 'abc' but
        # are distinct composite identities; both land in ONE transaction.
        h.conn.execute("BEGIN IMMEDIATE")
        h.conn.execute("INSERT INTO t VALUES ('ab','c','first')")
        h.conn.execute("INSERT INTO t VALUES ('a','bc','second')")
        h.conn.commit()
        wal = h.wal_copy()
        with WalChangeDecoder(
            base, wal, (TrackedTable("t", ("a", "b")),)) as dec:
            commits = list(dec.iter_commits())
        self.assertEqual(len(commits), 1)
        inserts = [e for ch in commits[0].changes for e in ch.events
                   if e.kind == "insert"]
        keys = sorted(
            (e.after["a"]["value"], e.after["b"]["value"]) for e in inserts)
        self.assertIn(("ab", "c"), keys)
        self.assertIn(("a", "bc"), keys)
        self.assertNotEqual(("ab", "c"), ("a", "bc"))
        self.assertEqual(len(inserts), 2)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.h = WalHarness()

    def tearDown(self):
        self.h.close()

    def test_unknown_column_rejected(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        h.conn.commit()
        base = h.baseline()
        wal = h.wal_copy()
        with self.assertRaises(InputValidationError) as ctx:
            with WalChangeDecoder(base, wal,
                                  (TrackedTable("t", ("ghost",)),)):
                pass
        self.assertIn("does not exist", ctx.exception.message)

    def test_nullable_non_unique_identity_rejected(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(v TEXT)")
        h.conn.execute("INSERT INTO t(v) VALUES ('a')")
        h.conn.commit()
        base = h.baseline()
        wal = h.wal_copy()
        with self.assertRaises(InputValidationError):
            with WalChangeDecoder(base, wal, (TrackedTable("t", ("v",)),)):
                pass

    def test_existing_null_identity_value_rejected(self):
        h = self.h
        # A nullable column with a UNIQUE index (SQLite permits NULLs in it):
        # the NOT NULL declaration guard rejects it before rows are scanned.
        h.conn.execute(
            "CREATE TABLE t(id INTEGER, v TEXT, UNIQUE(id))")
        h.conn.execute("INSERT INTO t(id, v) VALUES (NULL, 'a')")
        h.conn.commit()
        base = h.baseline()
        wal = h.wal_copy()
        with self.assertRaises(InputValidationError) as ctx:
            with WalChangeDecoder(base, wal, (TrackedTable("t", ("id",)),)):
                pass
        self.assertIn("NOT NULL", ctx.exception.message)

    def test_null_identity_row_scan_rejection(self):
        # The NULL row scan is a distinct guard from the column declaration.
        # Exercise it directly: a nullable UNIQUE column containing a NULL
        # passes the index/uniqueness declaration check, and the explicit row
        # scan must still reject it as an invalid identity.
        from wal_decode.schema import _validate_no_null_identity
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER, v TEXT, UNIQUE(id))")
        h.conn.execute("INSERT INTO t(id, v) VALUES (NULL, 'a')")
        h.conn.commit()
        base = h.baseline()
        uri = "file:" + base + "?immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            table = TrackedTable("t", ("id",))
            with self.assertRaises(InputValidationError) as ctx:
                _validate_no_null_identity(conn, table)
            self.assertIn("NULL identity", ctx.exception.message)
        finally:
            conn.close()

    def test_wrong_page_size_pair_rejected(self):
        h = self.h
        h.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        h.conn.commit()
        base = h.baseline()
        other = WalHarness(page_size=65536)
        other.conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        other.conn.execute("INSERT INTO t VALUES (1)")
        other.conn.commit()
        other_base = other.baseline()
        other.conn.execute("INSERT INTO t VALUES (2)")
        other.conn.commit()
        foreign_wal = other.wal_copy("foreign.wal")
        with self.assertRaises(WalFormatError) as ctx:
            with WalChangeDecoder(base, foreign_wal,
                                  (TrackedTable("t", ("id",)),)) as dec:
                list(dec.iter_commits())
        self.assertIn("page size", ctx.exception.message)
        other.close()

    def test_virtual_table_rejected_before_rows_read(self):
        h = self.h
        h.conn.execute("CREATE VIRTUAL TABLE ft USING fts5(body)")
        h.conn.execute("INSERT INTO ft(body) VALUES ('hello')")
        h.conn.commit()
        base = h.baseline()
        wal = h.wal_copy()
        with self.assertRaises(WalUnsupportedError) as ctx:
            with WalChangeDecoder(base, wal,
                                  (TrackedTable("ft", ("body",)),)):
                pass
        self.assertIn("virtual table", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
