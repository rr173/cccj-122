"""Tests for manifest verification and the command line entry point."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from io import StringIO
from unittest import mock

from wal_decode.cli import run as cli_run
from wal_decode.errors import InputValidationError
from wal_decode.manifest import (
    TrackedTable,
    build_manifest,
    load_manifest,
    verify_fingerprint,
)
from wal_decode.manifest import fingerprint_file


def make_wal_pair(tmp: str, page_size: int = 4096):
    db = os.path.join(tmp, "w.sqlite")
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"PRAGMA page_size={page_size}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1,'a')")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    base = os.path.join(tmp, "base.sqlite")
    shutil.copyfile(db, base)
    conn.execute("INSERT INTO t VALUES (2,'b')")
    conn.commit()
    wal = os.path.join(tmp, "cycle.wal")
    shutil.copyfile(db + "-wal", wal)
    conn.close()
    return base, wal


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base, self.wal = make_wal_pair(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_roundtrip_and_verify(self):
        tables = (TrackedTable("t", ("id",)),)
        manifest = build_manifest(self.base, self.wal, tables)
        path = os.path.join(self.tmp, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh)
        loaded = load_manifest(path)
        self.assertEqual(loaded.tables, tables)
        verify_fingerprint(fingerprint_file(self.base),
                           loaded.database, "database")
        verify_fingerprint(fingerprint_file(self.wal), loaded.wal, "WAL")

    def test_tampered_wal_fails_manifest_verification(self):
        tables = (TrackedTable("t", ("id",)),)
        manifest = build_manifest(self.base, self.wal, tables)
        # Mutate the WAL after manifesting; the digest guard must reject it.
        with open(self.wal, "r+b") as fh:
            fh.seek(40)
            fh.write(b"\x00")
        with self.assertRaises(InputValidationError) as ctx:
            verify_fingerprint(fingerprint_file(self.wal),
                               manifest.wal, "WAL")
        self.assertIn("SHA-256", ctx.exception.message)

    def test_length_mismatch_detected(self):
        tables = (TrackedTable("t", ("id",)),)
        manifest = build_manifest(self.base, self.wal, tables)
        with open(self.wal, "ab") as fh:
            fh.write(b"\x00")
        with self.assertRaises(InputValidationError) as ctx:
            verify_fingerprint(fingerprint_file(self.wal),
                               manifest.wal, "WAL")
        self.assertIn("length", ctx.exception.message)

    def test_bad_table_specs_rejected(self):
        with self.assertRaises(InputValidationError):
            TrackedTable("t", ())
        with self.assertRaises(InputValidationError):
            TrackedTable("t", ("a", "a"))
        with self.assertRaises(InputValidationError):
            TrackedTable('t"; DROP TABLE x;--', ("a",))

    def test_manifest_version_guard(self):
        bad = {"manifest_version": 999,
               "database": {"path": self.base, "byte_length": 1,
                            "sha256": "x"},
               "wal": {"path": self.wal, "byte_length": 1, "sha256": "x"},
               "tracked_tables": [
                   {"table": "t", "identity_columns": ["id"]}]}
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w") as fh:
            json.dump(bad, fh)
        with self.assertRaises(InputValidationError):
            load_manifest(path)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base, self.wal = make_wal_pair(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifest(self):
        tables = (TrackedTable("t", ("id",)),)
        manifest = build_manifest(self.base, self.wal, tables)
        path = os.path.join(self.tmp, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh)
        return path

    def test_cli_writes_output_and_succeeds(self):
        manifest = self._manifest()
        out = os.path.join(self.tmp, "out")
        code = cli_run(["decode", "--manifest", manifest, "--out", out])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(out, "commits.jsonl")))
        with open(os.path.join(out, "status.json"), encoding="utf-8") as sf:
            status = json.load(sf)
        self.assertEqual(status["outcome"], "ok")
        self.assertNotIn("INCOMPLETE.txt", os.listdir(out))

    def test_cli_stdout_streams_jsonl(self):
        manifest = self._manifest()
        out_err = StringIO()
        with mock.patch("sys.stdout", new_callable=StringIO) as out, \
                mock.patch("sys.stderr", out_err):
            code = cli_run(["decode", "--manifest", manifest, "--stdout"])
            lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        self.assertEqual(code, 0)
        records = [json.loads(ln) for ln in lines]
        self.assertTrue(records and "commit_seq" in records[0])

    def test_cli_usage_error_without_output_target(self):
        manifest = self._manifest()
        code = cli_run(["decode", "--manifest", manifest])
        self.assertEqual(code, 2)

    def test_cli_corrupt_wal_exit_3_and_no_commits_file(self):
        # Rebuild the manifest around the corrupted file so rejection is
        # internal WAL verification, not the digest front door.
        with open(self.wal, "r+b") as fh:
            fh.seek(32 + 16)  # first frame checksum
            fh.write(b"\x00\x00\x00\x00")
        tables = (TrackedTable("t", ("id",)),)
        manifest = build_manifest(self.base, self.wal, tables)
        path = os.path.join(self.tmp, "manifest.json")
        with open(path, "w") as fh:
            json.dump(manifest.to_dict(), fh)
        out = os.path.join(self.tmp, "out_corrupt")
        code = cli_run(["decode", "--manifest", path, "--out", out])
        self.assertEqual(code, 3)
        # The output directory may exist but never holds a commits.jsonl.
        self.assertFalse(os.path.exists(os.path.join(out, "commits.jsonl")))

    def test_cli_incomplete_exit_10_with_marker(self):
        with open(self.wal, "rb") as wf:
            full = wf.read()
        cut = len(full) - 50
        trunc = os.path.join(self.tmp, "trunc.wal")
        with open(trunc, "wb") as fh:
            fh.write(full[:cut])
        tables = (TrackedTable("t", ("id",)),)
        manifest = build_manifest(self.base, trunc, tables)
        path = os.path.join(self.tmp, "manifest_trunc.json")
        with open(path, "w") as fh:
            json.dump(manifest.to_dict(), fh)
        out = os.path.join(self.tmp, "out_trunc")
        code = cli_run(["decode", "--manifest", path, "--out", out])
        self.assertEqual(code, 10)
        self.assertTrue(os.path.exists(os.path.join(out, "INCOMPLETE.txt")))
        with open(os.path.join(out, "status.json"), encoding="utf-8") as sf:
            status = json.load(sf)
        self.assertEqual(status["outcome"], "incomplete")
        # Committed work before the tail is still emitted.
        with open(os.path.join(out, "commits.jsonl"), encoding="utf-8") as cf:
            commits = [json.loads(ln) for ln in cf if ln.strip()]
        self.assertEqual(len(commits), 0)  # sole commit was the cut frame


    def test_cli_no_command_is_usage_error(self):
        code = cli_run([])
        self.assertEqual(code, 2)

    def test_build_manifest_subcommand(self):
        output = os.path.join(self.tmp, "m.json")
        code = cli_run([
            "build-manifest", "--db", self.base, "--wal", self.wal,
            "--table", "t=id", "--output", output,
        ])
        self.assertEqual(code, 0)
        manifest = load_manifest(output)
        self.assertEqual(manifest.tables, (TrackedTable("t", ("id",)),))
        self.assertEqual(manifest.database.sha256,
                         fingerprint_file(self.base).sha256)
        self.assertEqual(manifest.wal.sha256,
                         fingerprint_file(self.wal).sha256)


if __name__ == "__main__":
    unittest.main()
