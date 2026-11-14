"""Tests for the warehouse_backup module.

Every restore is verified against an independently computed whole-file digest
and, for the business-warehouse tests, through the original xbrl_ingest query
entry points -- never only through the backup module's own check command.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from warehouse_backup import (  # noqa: E402
    BackupStore,
    InvocationError,
    StoreCorruptionError,
    apply_prune,
    check_backups,
    create_backup,
    plan_prune,
    restore_backup,
)
from warehouse_backup import service  # noqa: E402
from warehouse_backup.format import CHUNK_SIZE  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(PROJECT, "demo", "xbrl_fixtures")


def sha256_file(path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def make_db(path, rows=2000, payload=100, wal=False):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO items VALUES (?, ?)",
                     [(i, f"{i:06d}" + "x" * payload) for i in range(rows)])
    conn.commit()
    return conn


class BackupTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="warehouse_backup_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "warehouse.sqlite")
        self.store_dir = os.path.join(self.tmp, "backups")
        self.store = BackupStore(self.store_dir)

    def target(self, name="restored.sqlite"):
        return os.path.join(self.tmp, name)

    def chunk_files(self):
        return [p for _d, p, _s in self.store.iter_chunks()]

    def corrupt_some_chunk(self, layer_filter=None):
        """Flip one byte of the first chunk file; return its path."""
        victim = self.chunk_files()[0]
        with open(victim, "r+b") as handle:
            first = handle.read(1)
            handle.seek(0)
            handle.write(bytes([first[0] ^ 0xFF]))
        return victim


class CreateRestoreTest(BackupTestBase):
    def test_root_backup_and_restore_roundtrip(self):
        conn = make_db(self.db, wal=True)
        report = create_backup(self.store, self.db)
        self.assertEqual(report.backup_id, 1)
        self.assertIsNone(report.parent)
        self.assertGreater(report.chunk_count, 0)
        # The source stays usable and is not modified structurally.
        conn.execute("INSERT INTO items VALUES (99999, 'after')")
        conn.commit()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"items"})
        conn.close()

        out = self.target()
        restored = restore_backup(self.store, 1, out)
        # Independent whole-file digest equals the manifest's image digest.
        self.assertEqual(sha256_file(out), report.image_sha256)
        self.assertEqual(restored.integrity_check, "ok")
        check = sqlite3.connect(out)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                         2000)
        check.close()

    def test_incremental_dedupe_and_identical_rebackup(self):
        conn = make_db(self.db)
        root = create_backup(self.store, self.db)
        files_after_root = len(self.chunk_files())
        self.assertEqual(files_after_root, root.new_chunk_files)

        conn.execute("UPDATE items SET v='changed' WHERE id=7")
        conn.commit()
        child = create_backup(self.store, self.db, parent=root.backup_id)
        self.assertLess(child.layer_chunks, child.chunk_count)
        self.assertGreater(child.layer_chunks, 0)
        self.assertEqual(len(self.chunk_files()),
                         files_after_root + child.new_chunk_files)

        # Identical content again: new id, empty layer, no new chunk files.
        before = len(self.chunk_files())
        again = create_backup(self.store, self.db, parent=child.backup_id)
        self.assertEqual(again.backup_id, child.backup_id + 1)
        self.assertEqual(again.layer_chunks, 0)
        self.assertEqual(len(self.chunk_files()), before)
        self.assertEqual(again.image_sha256, child.image_sha256)
        conn.close()

    def test_three_level_chain_restore_uses_all_layers(self):
        conn = make_db(self.db, rows=3000)
        r1 = create_backup(self.store, self.db)
        conn.execute("UPDATE items SET v='one' WHERE id BETWEEN 100 AND 199")
        conn.commit()
        r2 = create_backup(self.store, self.db, parent=r1.backup_id)
        conn.execute("UPDATE items SET v='two' WHERE id BETWEEN 200 AND 299")
        conn.commit()
        r3 = create_backup(self.store, self.db, parent=r2.backup_id)
        conn.close()

        out = self.target()
        restore_backup(self.store, r3.backup_id, out)
        self.assertEqual(sha256_file(out), r3.image_sha256)
        check = sqlite3.connect(out)
        self.assertEqual(check.execute(
            "SELECT v FROM items WHERE id=150").fetchone()[0], "one")
        self.assertEqual(check.execute(
            "SELECT v FROM items WHERE id=250").fetchone()[0], "two")
        check.close()

        # Remove a chunk that only the root layer provides: the grandchild
        # restore must fail, proving the chain is really walked.
        resolution = service.resolve_chunks(service.load_chain(self.store, r3.backup_id))
        root_only = [entry for layer, entry in resolution.values()
                     if layer == r1.backup_id]
        self.assertTrue(root_only)
        self.store.chunk_path(root_only[0].sha256).unlink()
        with self.assertRaises(StoreCorruptionError):
            restore_backup(self.store, r3.backup_id, self.target("late.sqlite"))
        self.assertFalse(os.path.exists(self.target("late.sqlite")))

    def test_shrink_has_no_stale_tail(self):
        conn = make_db(self.db, rows=100)
        conn.execute("CREATE TABLE aux (id INTEGER PRIMARY KEY, blob TEXT)")
        conn.executemany("INSERT INTO aux VALUES (?, ?)",
                         [(i, "y" * 2000) for i in range(3000)])
        conn.commit()
        big = create_backup(self.store, self.db)
        conn.execute("DROP TABLE aux")
        conn.execute("VACUUM")
        conn.commit()
        small = create_backup(self.store, self.db, parent=big.backup_id)
        conn.close()
        self.assertLess(small.image_size, big.image_size)
        self.assertLess(small.chunk_count, big.chunk_count)

        out = self.target()
        restore_backup(self.store, small.backup_id, out)
        # Exact size: no parent tail bytes, no stale tail chunk content.
        self.assertEqual(os.path.getsize(out), small.image_size)
        self.assertEqual(sha256_file(out), small.image_sha256)
        check = sqlite3.connect(out)
        self.assertEqual(check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='aux'").fetchone()[0], 0)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                         100)
        check.close()

    def test_tail_chunk_length_change(self):
        conn = make_db(self.db, rows=100)
        root = create_backup(self.store, self.db)
        # Grow by less than one chunk: same chunk count, different tail bytes.
        conn.executemany("INSERT INTO items VALUES (?, ?)",
                         [(10000 + i, "z" * 40) for i in range(50)])
        conn.commit()
        child = create_backup(self.store, self.db, parent=root.backup_id)
        conn.close()
        self.assertEqual(child.image_size % CHUNK_SIZE == 0, False)
        out = self.target()
        restore_backup(self.store, child.backup_id, out)
        self.assertEqual(os.path.getsize(out), child.image_size)
        self.assertEqual(sha256_file(out), child.image_sha256)

    def test_create_unknown_parent_and_missing_source(self):
        make_db(self.db).close()
        with self.assertRaises(InvocationError):
            create_backup(self.store, self.db, parent=99)
        with self.assertRaises(InvocationError):
            create_backup(self.store, os.path.join(self.tmp, "nope.sqlite"))

    def test_unpublished_staging_is_not_a_backup(self):
        make_db(self.db).close()
        create_backup(self.store, self.db)
        staging = self.store.staging_path("manifest-9999.json")
        staging.write_text(json.dumps({"format": 1, "id": 2}) )
        ids = [m.id for m in self.store.load_all_manifests()]
        self.assertEqual(ids, [1])
        report = check_backups(self.store)
        self.assertTrue(report.ok)
        plan = apply_prune(self.store, (1,))
        self.assertIn(str(staging), plan.staging_files)
        self.assertFalse(staging.exists())


class ManifestValidationTest(BackupTestBase):
    def setUp(self):
        super().setUp()
        make_db(self.db).close()
        create_backup(self.store, self.db)
        self.valid = json.loads(
            self.store.manifest_path(1).read_text())

    def write_manifest(self, backup_id, data):
        self.store.manifest_path(backup_id).write_text(json.dumps(data))

    def assert_corrupt(self, data, backup_id=7):
        self.write_manifest(backup_id, data)
        with self.assertRaises(StoreCorruptionError):
            check_backups(self.store)

    def test_bad_format_version(self):
        data = dict(self.valid, id=7, format=99)
        self.assert_corrupt(data)

    def test_id_mismatch_with_filename(self):
        data = dict(self.valid, id=8)
        self.assert_corrupt(data, backup_id=7)

    def test_unknown_field_rejected(self):
        data = dict(self.valid, id=7, extra="nope")
        self.assert_corrupt(data)

    def test_missing_field_rejected(self):
        data = dict(self.valid, id=7)
        del data["image_sha256"]
        self.assert_corrupt(data)

    def test_bad_hex_digest_rejected(self):
        data = dict(self.valid, id=7, image_sha256="z" * 64)
        self.assert_corrupt(data)

    def test_chunk_index_out_of_range(self):
        data = dict(self.valid, id=7)
        entry = dict(data["chunks"][0])
        entry["index"] = data["chunk_count"]  # out of bounds
        data["chunks"] = [entry]
        self.assert_corrupt(data)

    def test_chunk_length_mismatch(self):
        data = dict(self.valid, id=7)
        entry = dict(data["chunks"][0])
        entry["length"] = entry["length"] - 1
        data["chunks"] = [entry]
        self.assert_corrupt(data)

    def test_chunk_size_must_be_fixed(self):
        data = dict(self.valid, id=7, chunk_size=32768)
        self.assert_corrupt(data)

    def test_chunk_count_must_match_image_size(self):
        data = dict(self.valid, id=7, chunk_count=self.valid["chunk_count"] + 1)
        self.assert_corrupt(data)

    def test_missing_parent_refused_everywhere(self):
        data = dict(self.valid, id=7, parent=42)
        self.write_manifest(7, data)
        with self.assertRaises(StoreCorruptionError):
            check_backups(self.store)
        with self.assertRaises(StoreCorruptionError):
            plan_prune(self.store, (1,))
        with self.assertRaises(StoreCorruptionError):
            apply_prune(self.store, (1,))
        with self.assertRaises(StoreCorruptionError):
            restore_backup(self.store, 7, self.target())

    def test_parent_cycle_refused(self):
        a = dict(self.valid, id=7, parent=8)
        b = dict(self.valid, id=8, parent=7)
        self.write_manifest(7, a)
        self.write_manifest(8, b)
        with self.assertRaises(StoreCorruptionError):
            check_backups(self.store)
        with self.assertRaises(StoreCorruptionError):
            plan_prune(self.store, (7,))
        with self.assertRaises(StoreCorruptionError):
            apply_prune(self.store, (7,))
        with self.assertRaises(StoreCorruptionError):
            restore_backup(self.store, 7, self.target())

    def test_self_parent_refused(self):
        data = dict(self.valid, id=7, parent=7)
        self.assert_corrupt(data)

    def test_symlink_chunk_refused(self):
        victim = self.chunk_files()[0]
        digest = victim.name[:64]
        victim.unlink()
        os.symlink("/etc/hostname", victim)
        with self.assertRaises(StoreCorruptionError):
            check_backups(self.store)
        with self.assertRaises(StoreCorruptionError):
            restore_backup(self.store, 1, self.target())

    def test_corrupt_chunk_detected(self):
        self.corrupt_some_chunk()
        with self.assertRaises(StoreCorruptionError):
            check_backups(self.store)
        with self.assertRaises(StoreCorruptionError):
            restore_backup(self.store, 1, self.target())

    def test_missing_chunk_detected(self):
        self.chunk_files()[0].unlink()
        with self.assertRaises(StoreCorruptionError):
            check_backups(self.store)
        with self.assertRaises(StoreCorruptionError):
            restore_backup(self.store, 1, self.target())

    def test_unexpected_files_refused(self):
        (self.store.manifest_dir / "README.txt").write_text("junk")
        with self.assertRaises(StoreCorruptionError):
            check_backups(self.store)


class RestoreTargetTest(BackupTestBase):
    def setUp(self):
        super().setUp()
        make_db(self.db).close()
        create_backup(self.store, self.db)

    def test_existing_target_refused_by_default(self):
        out = self.target()
        with open(out, "wb") as handle:
            handle.write(b"original")
        with self.assertRaises(InvocationError):
            restore_backup(self.store, 1, out)
        with open(out, "rb") as handle:
            self.assertEqual(handle.read(), b"original")

    def test_failed_replace_keeps_original_bytes(self):
        out = self.target()
        with open(out, "wb") as handle:
            handle.write(b"original-bytes")
        self.corrupt_some_chunk()
        with self.assertRaises(StoreCorruptionError):
            restore_backup(self.store, 1, out, replace=True)
        with open(out, "rb") as handle:
            self.assertEqual(handle.read(), b"original-bytes")
        # No staging files left behind next to the target.
        leftovers = [n for n in os.listdir(self.tmp) if ".restore-" in n]
        self.assertEqual(leftovers, [])

    def test_replace_success(self):
        out = self.target()
        with open(out, "wb") as handle:
            handle.write(b"old")
        report = restore_backup(self.store, 1, out, replace=True)
        self.assertEqual(sha256_file(out), report.image_sha256)

    def test_target_inside_backup_dir_refused(self):
        inside = os.path.join(self.store_dir, "out.sqlite")
        with self.assertRaises(InvocationError):
            restore_backup(self.store, 1, inside)

    def test_unknown_backup_id(self):
        with self.assertRaises(InvocationError):
            restore_backup(self.store, 42, self.target())


class PruneTest(BackupTestBase):
    def build_family(self):
        """1(root) <- 2 <- 3 chain; 4 branches off 1; 5 identical to 3."""
        conn = make_db(self.db, rows=3000)
        r1 = create_backup(self.store, self.db)
        conn.execute("UPDATE items SET v='b2' WHERE id BETWEEN 10 AND 19")
        conn.commit()
        r2 = create_backup(self.store, self.db, parent=r1.backup_id)
        conn.execute("UPDATE items SET v='b3' WHERE id BETWEEN 20 AND 29")
        conn.commit()
        r3 = create_backup(self.store, self.db, parent=r2.backup_id)
        r4 = create_backup(self.store, self.db, parent=r1.backup_id)
        r5 = create_backup(self.store, self.db, parent=r3.backup_id)
        conn.close()
        return r1, r2, r3, r4, r5

    def test_preview_keeps_dependencies_and_shared_chunks(self):
        r1, r2, r3, r4, r5 = self.build_family()
        files_before = len(self.chunk_files())
        plan = plan_prune(self.store, (r3.backup_id, r4.backup_id))
        # r1 and r2 are ancestors of kept r3; r1 is also ancestor of kept r4.
        self.assertEqual(set(plan.needed_dependencies), {r1.backup_id, r2.backup_id})
        self.assertEqual(set(plan.removable_manifests), {r5.backup_id})
        self.assertEqual(len(self.chunk_files()), files_before)  # preview only

    def test_apply_then_restores_still_work(self):
        r1, r2, r3, r4, r5 = self.build_family()
        apply_prune(self.store, (r3.backup_id, r4.backup_id))
        self.assertEqual(self.store.manifest_ids(),
                         [r1.backup_id, r2.backup_id, r3.backup_id, r4.backup_id])
        self.assertTrue(check_backups(self.store).ok)
        for backup_id, expect in ((r3.backup_id, r3.image_sha256),
                                  (r4.backup_id, r4.image_sha256)):
            out = self.target(f"r{backup_id}.sqlite")
            restore_backup(self.store, backup_id, out)
            self.assertEqual(sha256_file(out), expect)

    def test_shared_chunks_survive_prune(self):
        r1, r2, r3, r4, r5 = self.build_family()
        # Chunks shared between the r3 chain and the r4 branch must stay.
        apply_prune(self.store, (r3.backup_id, r4.backup_id))
        out = self.target()
        restore_backup(self.store, r4.backup_id, out)
        self.assertEqual(sha256_file(out), r4.image_sha256)

    def test_unreferenced_chunks_reclaimed(self):
        r1, r2, r3, r4, r5 = self.build_family()
        # Simulate a crashed creation: an orphan chunk nobody references.
        orphan = os.path.join(self.tmp, "orphan.bin")
        with open(orphan, "wb") as handle:
            handle.write(b"orphan-data")
        digest = sha256_file(orphan)
        with open(orphan, "rb") as handle:
            self.store.store_chunk(digest, handle.read())
        self.assertTrue(self.store.chunk_path(digest).exists())
        plan = apply_prune(self.store, (r3.backup_id, r4.backup_id))
        self.assertIn(digest, {d for d, _s in plan.unreferenced_chunks})
        self.assertFalse(self.store.chunk_path(digest).exists())

    def test_prune_refuses_corrupt_store_and_deletes_nothing(self):
        r1, r2, r3, r4, r5 = self.build_family()
        self.store.manifest_path(r1.backup_id).unlink()  # missing parent now
        files_before = sorted(p.name for p in self.chunk_files())
        with self.assertRaises(StoreCorruptionError):
            plan_prune(self.store, (r3.backup_id,))
        with self.assertRaises(StoreCorruptionError):
            apply_prune(self.store, (r3.backup_id,))
        files_after = sorted(p.name for p in self.chunk_files())
        self.assertEqual(files_before, files_after)
        self.assertTrue(self.store.manifest_path(r3.backup_id).exists())

    def test_unknown_keep_id(self):
        r1, r2, r3, r4, r5 = self.build_family()
        with self.assertRaises(InvocationError):
            plan_prune(self.store, (999,))


class WalConsistencyTest(BackupTestBase):
    """A live WAL writer advances two markers in one transaction; a backup
    taken mid-stream must restore with both markers equal."""

    def test_markers_stay_equal(self):
        db = os.path.join(self.tmp, "live.sqlite")
        setup = sqlite3.connect(db)
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE marker_a (n INTEGER NOT NULL)")
        setup.execute("CREATE TABLE marker_b (n INTEGER NOT NULL)")
        setup.execute("CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, pad TEXT)")
        setup.execute("INSERT INTO marker_a VALUES (0)")
        setup.execute("INSERT INTO marker_b VALUES (0)")
        setup.commit()
        setup.close()

        stop_file = os.path.join(self.tmp, "stop")
        writer_code = (
            "import sqlite3, os, time\n"
            f"db = {db!r}; stop = {stop_file!r}\n"
            "conn = sqlite3.connect(db, isolation_level=None)\n"
            "while not os.path.exists(stop):\n"
            "    conn.execute('BEGIN IMMEDIATE')\n"
            "    conn.execute('UPDATE marker_a SET n = n + 1')\n"
            "    conn.execute('UPDATE marker_b SET n = n + 1')\n"
            "    conn.execute(\"INSERT INTO log (pad) VALUES ('x' || hex(randomblob(64)))\")\n"
            "    conn.execute('COMMIT')\n"
            "    time.sleep(0.002)\n"
            "conn.close()\n"
        )
        writer = subprocess.Popen([sys.executable, "-I", "-c", writer_code])
        try:
            time.sleep(0.4)  # let the writer commit a number of transactions
            report = create_backup(self.store, db)
        finally:
            open(stop_file, "w").close()
            writer.wait(timeout=30)
        self.assertEqual(writer.returncode, 0)

        out = self.target()
        restore_backup(self.store, report.backup_id, out)
        check = sqlite3.connect(out)
        a = check.execute("SELECT n FROM marker_a").fetchone()[0]
        b = check.execute("SELECT n FROM marker_b").fetchone()[0]
        check.close()
        self.assertGreater(a, 0)
        self.assertEqual(a, b)

        # The production database kept working during and after the backup.
        live = sqlite3.connect(db)
        final_a, final_b = live.execute(
            "SELECT (SELECT n FROM marker_a), (SELECT n FROM marker_b)").fetchone()
        live.close()
        self.assertEqual(final_a, final_b)
        self.assertGreaterEqual(final_a, a)


class LockCoordinationTest(BackupTestBase):
    """A restore paused after resolving its dependencies must not lose the
    files it is about to read to a concurrent prune."""

    def test_prune_waits_for_restore(self):
        conn = make_db(self.db, rows=3000)
        r1 = create_backup(self.store, self.db)
        conn.execute("UPDATE items SET v='c2' WHERE id BETWEEN 5 AND 500")
        conn.commit()
        r2 = create_backup(self.store, self.db, parent=r1.backup_id)
        conn.execute("UPDATE items SET v='c3' WHERE id BETWEEN 501 AND 900")
        conn.commit()
        r3 = create_backup(self.store, self.db, parent=r2.backup_id)
        conn.close()

        gate = os.path.join(self.tmp, "gate")
        out = self.target()
        restore_proc = subprocess.Popen(
            [sys.executable, "-m", "warehouse_backup.cli", "restore",
             self.store_dir, "--id", str(r3.backup_id), "--target", out,
             "--pause-gate", gate],
            cwd=PROJECT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        acquired = gate + ".acquired"
        deadline = time.time() + 30
        while not os.path.exists(acquired):
            self.assertLess(time.time(), deadline, "restore never acquired deps")
            time.sleep(0.05)

        prune_proc = subprocess.Popen(
            [sys.executable, "-m", "warehouse_backup.cli", "prune",
             self.store_dir, "--keep", str(r1.backup_id), "--apply"],
            cwd=PROJECT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # While the restore holds its dependencies, the prune must not
        # complete and must not delete anything.
        time.sleep(2.0)
        self.assertIsNone(prune_proc.poll(),
                          "prune completed while restore held the lock")
        self.assertTrue(self.store.manifest_path(r3.backup_id).exists())
        self.assertTrue(self.store.manifest_path(r2.backup_id).exists())

        open(gate + ".go", "w").close()
        r_out, r_err = restore_proc.communicate(timeout=60)
        p_out, p_err = prune_proc.communicate(timeout=60)
        self.assertEqual(restore_proc.returncode, 0,
                         f"restore failed: {r_err}")
        self.assertEqual(prune_proc.returncode, 0, f"prune failed: {p_err}")
        # The restore read everything it needed before the prune ran.
        self.assertEqual(sha256_file(out), r3.image_sha256)
        # Afterwards the prune really did remove the unkept manifests.
        self.assertFalse(self.store.manifest_path(r3.backup_id).exists())
        self.assertFalse(self.store.manifest_path(r2.backup_id).exists())
        self.assertTrue(self.store.manifest_path(r1.backup_id).exists())


class CliExitCodeTest(BackupTestBase):
    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "warehouse_backup.cli", *argv],
            cwd=PROJECT, capture_output=True, text=True)

    def test_exit_codes(self):
        make_db(self.db).close()
        result = self.run_cli("create", self.store_dir, "--db", self.db)
        self.assertEqual(result.returncode, 0, result.stderr)

        result = self.run_cli("check", self.store_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        # Unknown id -> 2
        result = self.run_cli("restore", self.store_dir, "--id", "9",
                              "--target", self.target())
        self.assertEqual(result.returncode, 2)

        # Existing target without --replace -> 2
        out = self.target()
        open(out, "w").close()
        result = self.run_cli("restore", self.store_dir, "--id", "1",
                              "--target", out)
        self.assertEqual(result.returncode, 2)

        # Target in a nonexistent directory -> 4 (target write failure)
        result = self.run_cli("restore", self.store_dir, "--id", "1",
                              "--target", os.path.join(self.tmp, "no-such-dir",
                                                       "out.sqlite"))
        self.assertEqual(result.returncode, 4)
        self.assertIn("target-write error", result.stderr)

        # Corrupt the store -> check 3, restore 3, prune 3
        victim = self.chunk_files()[0]
        with open(victim, "r+b") as handle:
            handle.write(b"\xff")
        result = self.run_cli("check", self.store_dir)
        self.assertEqual(result.returncode, 3)
        result = self.run_cli("restore", self.store_dir, "--id", "1",
                              "--target", self.target("other.sqlite"))
        self.assertEqual(result.returncode, 3)
        result = self.run_cli("prune", self.store_dir, "--keep", "1")
        self.assertEqual(result.returncode, 3)
        result = self.run_cli("prune", self.store_dir, "--keep", "1", "--apply")
        self.assertEqual(result.returncode, 3)

    def test_create_bad_parent_exit_2(self):
        make_db(self.db).close()
        result = self.run_cli("create", self.store_dir, "--db", self.db,
                              "--parent", "5")
        self.assertEqual(result.returncode, 2)

    def test_prune_without_keep_exit_2(self):
        make_db(self.db).close()
        self.run_cli("create", self.store_dir, "--db", self.db)
        result = self.run_cli("prune", self.store_dir)
        self.assertEqual(result.returncode, 2)
        result = self.run_cli("prune", self.store_dir, "--apply")
        self.assertEqual(result.returncode, 2)
        # Nothing was deleted.
        result = self.run_cli("check", self.store_dir)
        self.assertEqual(result.returncode, 0)


class BusinessWarehouseTest(BackupTestBase):
    """Round-trip a real XBRL fact warehouse; verify through the original
    ingest/query entry points, not the backup module's own checks."""

    def test_facts_survive_backup_and_restore(self):
        from xbrl_ingest import XbrlIngestor

        dictionary = os.path.join(FIXTURES, "concepts.json")
        ingestor = XbrlIngestor(self.db, dictionary)
        report_a = ingestor.ingest_file(os.path.join(FIXTURES, "demo-A.xml"))
        report_b = ingestor.ingest_file(os.path.join(FIXTURES, "demo-B.xml"))
        self.assertEqual(ingestor.store.source_count(), 2)

        def snapshot(db_path):
            with XbrlIngestor(db_path, dictionary) as ing:
                conflicts = ing.query(concept_local="Revenue",
                                      period_kind="duration")
                instant = ing.query(concept_local="Revenue", period_kind="instant")
                nothing = ing.query(concept_local="Nothing")
                huge = ing.query(concept_local="Huge")
                note = ing.search_text("retail segment")
                sources = [(s.source.source_id, s.source.content_sha256)
                           for s in ing.sources()]
                traced = ing.trace_fact(report_a.source_id, 1)
                return {
                    "conflict_status": conflicts[0].status,
                    "conflict_values": sorted(c.value_text
                                              for c in conflicts[0].candidates),
                    "conflict_sources": sorted({c.source_id
                                                for c in conflicts[0].candidates}),
                    "instant_candidates": len(instant[0].candidates),
                    "nothing_nil": nothing[0].candidates[0].nil,
                    "huge_text": huge[0].candidates[0].value_text,
                    "huge_decimals": huge[0].candidates[0].decimals,
                    "note_texts": sorted(c.value_text
                                         for c in note[0].candidates),
                    "sources": sources,
                    "traced_line": traced.location.line,
                }

        before = snapshot(self.db)
        self.assertEqual(before["conflict_status"], "conflict")
        self.assertEqual(before["conflict_values"], ["1234.50", "1250.00"])
        self.assertEqual(before["instant_candidates"], 3)
        self.assertTrue(before["nothing_nil"])
        self.assertEqual(
            before["huge_text"],
            "1234567890123456789012345678901234567890")
        self.assertEqual(before["huge_decimals"], "INF")
        self.assertEqual(before["note_texts"],
                         ["  retail segment  ", "retail segment"])
        ingestor.close()

        report = create_backup(self.store, self.db)
        out = self.target()
        restore_backup(self.store, report.backup_id, out)
        after = snapshot(out)
        self.assertEqual(before, after)
        self.assertEqual({report_a.source_id, report_b.source_id},
                         set(sid for sid, _ in after["sources"]))


if __name__ == "__main__":
    unittest.main()
