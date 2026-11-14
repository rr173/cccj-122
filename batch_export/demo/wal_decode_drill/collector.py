"""Collection helper for the WAL drill: produce a real baseline + raw WAL.

The procedure mirrors the documented collection contract exactly:

1. open one write connection, switch to WAL, disable auto-checkpoint;
2. after the baseline content is loaded, force ``wal_checkpoint(TRUNCATE)`` so
   every committed page lives in the main database and the WAL is zeroed;
3. copy the main database *while the same write connection stays alive*;
4. run the scenario transactions on that connection -- the fresh WAL of this
   one checkpoint cycle is the artifact handed to the decoder;
5. never copy a WAL that was overwritten by a checkpoint cycle, and never copy
   a file that is still being mutated (the copy happens at a defined quiescent
   point after each COMMIT).

Only standard SQLite behaviour is used; the decoder never sees this helper.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class CollectedArtifacts:
    db_path: str
    wal_path: str
    manifest_path: str


class WalCollector:
    """One live write connection plus frozen baseline/WAL artifacts."""

    def __init__(self, workdir: str, name: str, page_size: int = 4096):
        os.makedirs(workdir, exist_ok=True)
        self.live_db = os.path.join(workdir, f"{name}.live.sqlite")
        self.baseline_db = os.path.join(workdir, f"{name}.baseline.sqlite")
        self.cycle_wal = os.path.join(workdir, f"{name}.cycle.sqlite-wal")
        self.page_size = page_size
        # isolation_level=None -> explicit BEGIN/COMMIT, no implicit magic.
        self.conn = sqlite3.connect(self.live_db, isolation_level=None)
        self.conn.execute(f"PRAGMA page_size={page_size}")
        mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode.lower() == "wal", mode
        self.conn.execute("PRAGMA wal_autocheckpoint=0")

    def checkpoint_truncate_and_copy_baseline(self) -> None:
        """TRUNCATE checkpoint, then freeze the main database as baseline."""
        result = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # busy=0, log frames checkpointed, checkpointed status 0/1
        if result[0] != 0:
            raise RuntimeError(f"TRUNCATE checkpoint reported busy: {result}")
        if os.path.exists(self.live_db + "-wal") and \
                os.path.getsize(self.live_db + "-wal") not in (0,):
            # TRUNCATE leaves a zero-length WAL; anything else is unexpected.
            raise RuntimeError("WAL was not truncated to zero length")
        shutil.copyfile(self.live_db, self.baseline_db)

    def freeze_cycle_wal(self) -> None:
        """Copy the current WAL bytes at a quiescent point (no open txn)."""
        if self.conn.in_transaction:
            raise RuntimeError("cannot freeze the WAL while a transaction is open")
        shutil.copyfile(self.live_db + "-wal", self.cycle_wal)

    def write_manifest(self, path: str, tracked_tables: list[dict],
                       note: str = "") -> dict:
        def fingerprint(p: str) -> dict:
            return {"path": os.path.abspath(p),
                    "byte_length": os.path.getsize(p),
                    "sha256": sha256_file(p)}
        manifest = {
            "manifest_version": 1,
            "database": fingerprint(self.baseline_db),
            "wal": fingerprint(self.cycle_wal),
            "tracked_tables": tracked_tables,
            "note": note,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        return manifest

    def close(self) -> None:
        self.conn.close()
