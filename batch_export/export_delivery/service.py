"""Export delivery service: submission, dispatch, receipts, revoke, recovery.

All state lives in SQLite, so a new ExportService instance over the same
database file transparently resumes in-flight exports (process recovery).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .models import (
    ExportJob,
    ExportRequest,
    InvalidStateError,
    JobStatus,
    Receipt,
    ReceiptOutcome,
    ShardInfo,
    ShardPayload,
    ShardStatus,
    SourceDataChangedError,
    UnknownJobError,
    shard_digest,
)
from .store import StateStore, utcnow
from .warehouse import LocalWarehouse


class ExportService:
    def __init__(self, db_path: str, warehouse: LocalWarehouse):
        self.store = StateStore(db_path)
        self.warehouse = warehouse

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "ExportService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- submission -----------------------------------------------------------

    def submit_export(self, request: ExportRequest) -> ExportJob:
        """Create an export job, or return the existing one for the same request.

        The job id is derived from the canonical request, so identical requests
        share a stable identity and never create duplicate exports.
        """
        job_id = request.request_id
        existing = self.store.get_job(job_id)
        if existing is not None:
            return existing

        # Pin the query version now: the manifest is cut from this version's
        # data and every later dispatch re-verifies against it.
        rows = self.warehouse.query(
            version=request.query_version,
            projection=request.projection,
            filters=request.filters,
        )
        now = utcnow()
        shards = [
            ShardInfo(
                job_id=job_id,
                ordinal=ordinal,
                row_offset=start,
                row_count=len(chunk),
                digest=shard_digest(job_id, ordinal, chunk),
                status=ShardStatus.PENDING,
                send_count=0,
            )
            for ordinal, start in enumerate(range(0, len(rows), request.shard_size))
            for chunk in (rows[start:start + request.shard_size],)
        ]
        if shards:
            status, completed_at = JobStatus.PREPARED, None
        else:
            # Empty result: zero shards means zero outstanding receipts, so the
            # export is complete as soon as its (empty) manifest is recorded.
            status, completed_at = JobStatus.DELIVERED, now
        job = ExportJob(
            job_id=job_id,
            request=request,
            status=status,
            shard_count=len(shards),
            row_count=len(rows),
            created_at=now,
            updated_at=now,
            completed_at=completed_at,
        )
        try:
            with self.store.transaction():
                self.store.insert_job(job)
                self.store.insert_shards(shards)
        except sqlite3.IntegrityError:
            # Lost a race with a concurrent identical submission: return it.
            return self.get_job(job_id)
        return job

    # -- dispatch ---------------------------------------------------------------

    def dispatch_unacknowledged(
        self, job_id: str, limit: Optional[int] = None
    ) -> list[ShardPayload]:
        """Send every shard that has not been acknowledged yet.

        Used for the initial delivery, for partial retries (only unconfirmed
        shards go out again) and for recovery after a restart.
        """
        job = self.get_job(job_id)
        if job.status not in (JobStatus.PREPARED, JobStatus.DELIVERING):
            raise InvalidStateError(
                f"cannot dispatch shards for job {job_id} in status {job.status.value}"
            )
        pending = self.store.unacknowledged_shards(job_id)
        if limit is not None:
            pending = pending[:limit]
        payloads = []
        for shard in pending:
            rows = self.warehouse.query(
                version=job.request.query_version,
                projection=job.request.projection,
                filters=job.request.filters,
                offset=shard.row_offset,
                limit=shard.row_count,
            )
            digest = shard_digest(job_id, shard.ordinal, rows)
            if digest != shard.digest:
                raise SourceDataChangedError(
                    f"shard {shard.ordinal} of job {job_id} no longer matches the"
                    f" export manifest; pinned query version"
                    f" {job.request.query_version!r} must have changed"
                )
            attempt = self.store.record_send(job_id, shard.ordinal)
            payloads.append(
                ShardPayload(
                    job_id=job_id,
                    shard_ordinal=shard.ordinal,
                    receiver=job.request.receiver,
                    query_version=job.request.query_version,
                    projection=job.request.projection,
                    rows=tuple(tuple(r) for r in rows),
                    digest=digest,
                    attempt=attempt,
                )
            )
        if payloads and job.status == JobStatus.PREPARED:
            self.store.set_job_status(job_id, JobStatus.DELIVERING)
        return payloads

    # -- receipts ---------------------------------------------------------------

    def record_receipt(self, receipt: Receipt) -> ReceiptOutcome:
        """Record a receiver receipt; duplicates and out-of-order are tolerated.

        A receipt whose digest contradicts the manifest moves the shard and the
        job into CONFLICT. Every receipt is persisted for audit, including
        duplicates and rejections.
        """
        with self.store.transaction():
            job = self.store.get_job(receipt.job_id)
            if job is None:
                raise UnknownJobError(receipt.job_id)
            if self.store.receipt_exists(receipt.receipt_id):
                return ReceiptOutcome.DUPLICATE  # exact replay, recorded once
            shard = self.store.get_shard(receipt.job_id, receipt.shard_ordinal)

            def log(outcome: ReceiptOutcome, note: Optional[str] = None) -> ReceiptOutcome:
                self.store.insert_receipt(receipt, outcome, note)
                return outcome

            if shard is None:
                return log(ReceiptOutcome.REJECTED, "shard not in export manifest")
            if receipt.receiver != job.request.receiver:
                return log(ReceiptOutcome.REJECTED, "receiver mismatch")
            if job.status == JobStatus.REVOKED:
                return log(ReceiptOutcome.REJECTED, "job revoked")
            if shard.status == ShardStatus.ACKED:
                return log(ReceiptOutcome.DUPLICATE, "shard already acknowledged")
            if shard.status == ShardStatus.PENDING:
                return log(ReceiptOutcome.REJECTED, "shard not dispatched yet")
            if shard.status == ShardStatus.CONFLICT:
                return log(ReceiptOutcome.REJECTED, "shard in conflict")
            # shard is SENT: this receipt is the first answer for it
            if receipt.digest != shard.digest:
                self.store.set_shard_status(receipt.job_id, shard.ordinal, ShardStatus.CONFLICT)
                self.store.set_job_status(receipt.job_id, JobStatus.CONFLICT)
                return log(
                    ReceiptOutcome.CONFLICT,
                    f"digest mismatch: manifest={shard.digest} receipt={receipt.digest}",
                )
            self.store.set_shard_status(receipt.job_id, shard.ordinal, ShardStatus.ACKED)
            if self.store.all_shards_acked(receipt.job_id):
                self.store.set_job_status(receipt.job_id, JobStatus.DELIVERED, completed=True)
            return log(ReceiptOutcome.ACCEPTED)

    # -- revocation ---------------------------------------------------------------

    def revoke(self, job_id: str) -> ExportJob:
        """Revoke a delivery that has not completed yet (idempotent)."""
        job = self.get_job(job_id)
        if job.status == JobStatus.DELIVERED:
            raise InvalidStateError("cannot revoke a completed export")
        if job.status != JobStatus.REVOKED:
            self.store.set_job_status(job_id, JobStatus.REVOKED)
        return self.get_job(job_id)

    # -- recovery / audit ---------------------------------------------------------

    def recover_incomplete(self) -> list[ExportJob]:
        """Jobs that a restarted process should resume (PREPARED/DELIVERING)."""
        return self.store.list_jobs(statuses=(JobStatus.PREPARED, JobStatus.DELIVERING))

    def get_job(self, job_id: str) -> ExportJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        return job

    def list_jobs(self) -> list[ExportJob]:
        return self.store.list_jobs()

    def manifest(self, job_id: str) -> list[ShardInfo]:
        self.get_job(job_id)
        return self.store.list_shards(job_id)

    def audit_trail(self, job_id: str) -> dict:
        """Query definition, shard manifest, sends and receipts for one export."""
        job = self.get_job(job_id)
        return {
            "job": job,
            "request": job.request.canonical(),
            "manifest": self.store.list_shards(job_id),
            "receipts": self.store.list_receipts(job_id),
            "sends": self.store.list_sends(job_id),
        }
