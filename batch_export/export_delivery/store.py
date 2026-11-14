"""SQLite-backed state store for export jobs, manifests, sends and receipts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from .models import ExportJob, ExportRequest, JobStatus, ShardInfo, ShardStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS export_jobs (
    job_id        TEXT PRIMARY KEY,
    request_json  TEXT NOT NULL,
    query_version TEXT NOT NULL,
    receiver      TEXT NOT NULL,
    status        TEXT NOT NULL,
    shard_count   INTEGER NOT NULL,
    row_count     INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT
);
CREATE TABLE IF NOT EXISTS shards (
    job_id      TEXT NOT NULL REFERENCES export_jobs(job_id),
    ordinal     INTEGER NOT NULL,
    row_offset  INTEGER NOT NULL,
    row_count   INTEGER NOT NULL,
    digest      TEXT NOT NULL,
    status      TEXT NOT NULL,
    send_count  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (job_id, ordinal)
);
CREATE TABLE IF NOT EXISTS send_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    attempt    INTEGER NOT NULL,
    sent_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id    TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    shard_ordinal INTEGER NOT NULL,
    digest        TEXT NOT NULL,
    receiver      TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    note          TEXT,
    received_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_job ON receipts(job_id, shard_ordinal);
CREATE INDEX IF NOT EXISTS idx_send_log_job ON send_log(job_id, ordinal);
"""


class StateStore:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- jobs ---------------------------------------------------------------

    def insert_job(self, job: ExportJob) -> None:
        self._conn.execute(
            "INSERT INTO export_jobs (job_id, request_json, query_version, receiver,"
            " status, shard_count, row_count, created_at, updated_at, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job.job_id,
                job.request.to_json(),
                job.request.query_version,
                job.request.receiver,
                job.status.value,
                job.shard_count,
                job.row_count,
                job.created_at,
                job.updated_at,
                job.completed_at,
            ),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> ExportJob:
        return ExportJob(
            job_id=row["job_id"],
            request=ExportRequest.from_json(row["request_json"]),
            status=JobStatus(row["status"]),
            shard_count=row["shard_count"],
            row_count=row["row_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def get_job(self, job_id: str) -> Optional[ExportJob]:
        row = self._conn.execute(
            "SELECT * FROM export_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._job_from_row(row) if row else None

    def list_jobs(self, statuses: Optional[tuple[JobStatus, ...]] = None) -> list[ExportJob]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self._conn.execute(
                f"SELECT * FROM export_jobs WHERE status IN ({marks})"
                " ORDER BY created_at, job_id",
                [s.value for s in statuses],
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM export_jobs ORDER BY created_at, job_id"
            ).fetchall()
        return [self._job_from_row(r) for r in rows]

    def set_job_status(self, job_id: str, status: JobStatus, *, completed: bool = False) -> None:
        now = utcnow()
        if completed:
            self._conn.execute(
                "UPDATE export_jobs SET status = ?, updated_at = ?, completed_at = ?"
                " WHERE job_id = ?",
                (status.value, now, now, job_id),
            )
        else:
            self._conn.execute(
                "UPDATE export_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (status.value, now, job_id),
            )

    # -- shards (the manifest) ------------------------------------------------

    def insert_shards(self, shards: list[ShardInfo]) -> None:
        now = utcnow()
        self._conn.executemany(
            "INSERT INTO shards (job_id, ordinal, row_offset, row_count, digest,"
            " status, send_count, updated_at) VALUES (?,?,?,?,?,?,0,?)",
            [
                (s.job_id, s.ordinal, s.row_offset, s.row_count, s.digest,
                 s.status.value, now)
                for s in shards
            ],
        )

    @staticmethod
    def _shard_from_row(row: sqlite3.Row) -> ShardInfo:
        return ShardInfo(
            job_id=row["job_id"],
            ordinal=row["ordinal"],
            row_offset=row["row_offset"],
            row_count=row["row_count"],
            digest=row["digest"],
            status=ShardStatus(row["status"]),
            send_count=row["send_count"],
        )

    def list_shards(self, job_id: str) -> list[ShardInfo]:
        rows = self._conn.execute(
            "SELECT * FROM shards WHERE job_id = ? ORDER BY ordinal", (job_id,)
        ).fetchall()
        return [self._shard_from_row(r) for r in rows]

    def get_shard(self, job_id: str, ordinal: int) -> Optional[ShardInfo]:
        row = self._conn.execute(
            "SELECT * FROM shards WHERE job_id = ? AND ordinal = ?",
            (job_id, ordinal),
        ).fetchone()
        return self._shard_from_row(row) if row else None

    def set_shard_status(self, job_id: str, ordinal: int, status: ShardStatus) -> None:
        self._conn.execute(
            "UPDATE shards SET status = ?, updated_at = ? WHERE job_id = ? AND ordinal = ?",
            (status.value, utcnow(), job_id, ordinal),
        )

    def unacknowledged_shards(self, job_id: str) -> list[ShardInfo]:
        rows = self._conn.execute(
            "SELECT * FROM shards WHERE job_id = ? AND status IN ('PENDING','SENT')"
            " ORDER BY ordinal",
            (job_id,),
        ).fetchall()
        return [self._shard_from_row(r) for r in rows]

    def all_shards_acked(self, job_id: str) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM shards WHERE job_id = ? AND status != 'ACKED'",
            (job_id,),
        ).fetchone()
        return row["c"] == 0

    # -- send log ---------------------------------------------------------------

    def record_send(self, job_id: str, ordinal: int) -> int:
        """Mark a shard sent and append to the send log; returns the attempt #."""
        with self.transaction():
            row = self._conn.execute(
                "SELECT send_count FROM shards WHERE job_id = ? AND ordinal = ?",
                (job_id, ordinal),
            ).fetchone()
            attempt = row["send_count"] + 1
            self._conn.execute(
                "UPDATE shards SET status = ?, send_count = ?, updated_at = ?"
                " WHERE job_id = ? AND ordinal = ?",
                (ShardStatus.SENT.value, attempt, utcnow(), job_id, ordinal),
            )
            self._conn.execute(
                "INSERT INTO send_log (job_id, ordinal, attempt, sent_at) VALUES (?,?,?,?)",
                (job_id, ordinal, attempt, utcnow()),
            )
        return attempt

    def list_sends(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM send_log WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- receipts ---------------------------------------------------------------

    def receipt_exists(self, receipt_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM receipts WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()
        return row is not None

    def insert_receipt(self, receipt, outcome, note: Optional[str]) -> None:
        self._conn.execute(
            "INSERT INTO receipts (receipt_id, job_id, shard_ordinal, digest, receiver,"
            " outcome, note, received_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                receipt.receipt_id,
                receipt.job_id,
                receipt.shard_ordinal,
                receipt.digest,
                receipt.receiver,
                outcome.value,
                note,
                utcnow(),
            ),
        )

    def list_receipts(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM receipts WHERE job_id = ? ORDER BY received_at, receipt_id",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]
