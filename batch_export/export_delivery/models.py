"""Core domain models for the auditable batch export delivery service."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ExportError(Exception):
    """Base error for the export delivery service."""


class UnknownJobError(ExportError):
    """Raised when a receipt or operation references an unknown export job."""


class InvalidStateError(ExportError):
    """Raised when an operation is not allowed in the job's current state."""


class SourceDataChangedError(ExportError):
    """Raised when warehouse data no longer matches the export manifest.

    This is the loud failure that prevents changed source data from being
    silently mixed into an export that has already started.
    """


class UnknownVersionError(ExportError):
    """Raised when the warehouse does not know the requested query version."""


class UnknownFieldError(ExportError):
    """Raised when a projection/filter references a non-existent field."""


# ---------------------------------------------------------------------------
# State machines
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PREPARED = "PREPARED"        # manifest generated, nothing sent yet
    DELIVERING = "DELIVERING"    # at least one shard dispatched
    DELIVERED = "DELIVERED"      # all shard receipts matched the manifest (terminal)
    CONFLICT = "CONFLICT"        # a receipt digest contradicted the manifest
    REVOKED = "REVOKED"          # delivery revoked before completion (terminal)


TERMINAL_JOB_STATUSES = frozenset({JobStatus.DELIVERED, JobStatus.REVOKED})


class ShardStatus(str, Enum):
    PENDING = "PENDING"    # in manifest, never dispatched
    SENT = "SENT"          # dispatched, awaiting receipt
    ACKED = "ACKED"        # receipt matched the manifest digest
    CONFLICT = "CONFLICT"  # receipt digest contradicted the manifest


class ReceiptOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"    # receipt applied, shard acknowledged
    DUPLICATE = "DUPLICATE"  # shard already acknowledged / receipt replayed
    CONFLICT = "CONFLICT"    # digest mismatch -> conflict state
    REJECTED = "REJECTED"    # not applicable (revoked, unknown shard, ...)


# ---------------------------------------------------------------------------
# Canonicalisation helpers
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """Deterministic JSON encoding used for identities and digests."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ExportRequest:
    """What the caller submits: query version, projection, filters, receiver.

    The request identity is a hash of its canonical form, so the same logical
    request always maps to the same export job (stable request identity).
    """

    query_version: str
    projection: tuple[str, ...]
    filters: dict[str, Any]
    receiver: str
    shard_size: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "projection", tuple(self.projection))
        if not self.projection:
            raise ValueError("projection must not be empty")
        if self.shard_size < 1:
            raise ValueError("shard_size must be >= 1")
        if not self.receiver:
            raise ValueError("receiver must not be empty")
        canonical_json(self.filters)  # filters must be canonicalisable

    def canonical(self) -> dict:
        return {
            "query_version": self.query_version,
            "projection": list(self.projection),
            "filters": self.filters,
            "receiver": self.receiver,
            "shard_size": self.shard_size,
        }

    @property
    def request_id(self) -> str:
        digest = hashlib.sha256(canonical_json(self.canonical()).encode("utf-8")).hexdigest()
        return f"exp_{digest[:32]}"

    def to_json(self) -> str:
        return canonical_json(self.canonical())

    @staticmethod
    def from_json(payload: str) -> "ExportRequest":
        return ExportRequest(**json.loads(payload))


def shard_digest(request_id: str, ordinal: int, rows: list) -> str:
    """Per-shard summary bound to this export's identity and shard ordinal."""
    payload = canonical_json({
        "request_id": request_id,
        "shard_ordinal": ordinal,
        "row_count": len(rows),
        "rows": rows,
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Wire / read models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShardPayload:
    """One shard handed to the receiver."""

    job_id: str
    shard_ordinal: int
    receiver: str
    query_version: str
    projection: tuple[str, ...]
    rows: tuple
    digest: str
    attempt: int


@dataclass(frozen=True)
class Receipt:
    """Receiver acknowledgement carrying the shard identity."""

    receipt_id: str
    job_id: str
    shard_ordinal: int
    digest: str
    receiver: str

    @staticmethod
    def issue(job_id: str, shard_ordinal: int, digest: str, receiver: str) -> "Receipt":
        return Receipt(
            receipt_id=f"rcpt_{uuid.uuid4().hex[:24]}",
            job_id=job_id,
            shard_ordinal=shard_ordinal,
            digest=digest,
            receiver=receiver,
        )


@dataclass(frozen=True)
class ExportJob:
    job_id: str
    request: ExportRequest
    status: JobStatus
    shard_count: int
    row_count: int
    created_at: str
    updated_at: str
    completed_at: Optional[str]


@dataclass(frozen=True)
class ShardInfo:
    job_id: str
    ordinal: int
    row_offset: int
    row_count: int
    digest: str
    status: ShardStatus
    send_count: int
