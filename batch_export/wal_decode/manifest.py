"""Collection manifest and read-only input validation.

The collection procedure (documented in the package README) is the collector's
responsibility:

1. force a ``TRUNCATE`` checkpoint so the main database holds every committed
   page, then copy the main database;
2. keep the same write connection alive, disable automatic checkpointing and
   gather the fresh WAL of one checkpoint cycle.

The manifest records the length and SHA-256 of *both* files and the ordered
identity columns of every tracked table. This tool verifies the files against
the manifest and never writes beside the inputs: no ``-shm`` is created or
used, and the original database+WAL pair is never handed to SQLite for
automatic recovery. A matching digest proves the handed-in files match the
manifest -- it cannot prove a WAL pairs with an arbitrary main database; that
belongs to collection discipline.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from .errors import InputValidationError

MANIFEST_VERSION = 1
_HASH_CHUNK = 1024 * 1024
# SQLite identifiers: letters/underscore start, then word or '$'. Quoted
# identifiers in DDL are out of scope; table and column names are handled as
# real identifiers, never concatenated into arbitrary SQL.
_IDENT_RE = re.compile(r"^[A-Za-z_\u0080-￿][A-Za-z0-9_$\u0080-￿]*$")


def quote_identifier(name: str) -> str:
    """Return a safely double-quoted SQL identifier after validating it."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise InputValidationError(
            f"illegal SQL identifier {name!r}; table and column names must be plain "
            "unquoted SQLite identifiers"
        )
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class TrackedTable:
    name: str
    identity_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.identity_columns:
            raise InputValidationError(
                f"tracked table {self.name!r} must declare at least one identity column"
            )
        if len(set(self.identity_columns)) != len(self.identity_columns):
            raise InputValidationError(
                f"duplicate identity column for table {self.name!r}: {self.identity_columns}"
            )
        quote_identifier(self.name)
        for column in self.identity_columns:
            quote_identifier(column)


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class CollectionManifest:
    database: FileFingerprint
    wal: FileFingerprint
    tables: tuple[TrackedTable, ...]
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "manifest_version": MANIFEST_VERSION,
            "database": self.database.__dict__,
            "wal": self.wal.__dict__,
            "tracked_tables": [
                {"table": t.name, "identity_columns": list(t.identity_columns)}
                for t in self.tables
            ],
            "note": self.note,
        }


def _fingerprint(path: str) -> FileFingerprint:
    if not os.path.isfile(path):
        raise InputValidationError(f"input file does not exist: {path}")
    if os.path.islink(path):
        raise InputValidationError(f"refusing to follow a symlinked input: {path}")
    digest = hashlib.sha256()
    length = 0
    # Read-only binary handle, chunked; never creates -shm/-wal sidecars.
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
    return FileFingerprint(path=os.path.abspath(path), byte_length=length,
                           sha256=digest.hexdigest())


def load_manifest(path: str) -> CollectionManifest:
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputValidationError(f"manifest is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("manifest_version") != MANIFEST_VERSION:
        raise InputValidationError(
            f"manifest_version must be {MANIFEST_VERSION}; got "
            f"{data.get('manifest_version') if isinstance(data, dict) else 'non-object'}"
        )
    try:
        db = FileFingerprint(**data["database"])
        wal = FileFingerprint(**data["wal"])
        tables = tuple(
            TrackedTable(t["table"], tuple(t["identity_columns"]))
            for t in data["tracked_tables"]
        )
    except (KeyError, TypeError) as exc:
        raise InputValidationError(f"malformed manifest: {exc}") from exc
    if not tables:
        raise InputValidationError("manifest must list at least one tracked table")
    if len({t.name for t in tables}) != len(tables):
        raise InputValidationError("manifest lists the same tracked table twice")
    return CollectionManifest(db, wal, tables, data.get("note", ""))


def verify_fingerprint(actual: FileFingerprint, declared: FileFingerprint,
                       label: str) -> None:
    """Compare a freshly hashed input against the manifest entry."""
    if actual.byte_length != declared.byte_length:
        raise InputValidationError(
            f"{label} length mismatch: manifest says {declared.byte_length} bytes but "
            f"{actual.path} is {actual.byte_length} bytes"
        )
    if actual.sha256 != declared.sha256:
        raise InputValidationError(
            f"{label} SHA-256 mismatch: manifest says {declared.sha256} but "
            f"{actual.path} hashes to {actual.sha256}"
        )


def fingerprint_file(path: str) -> FileFingerprint:
    return _fingerprint(path)


def build_manifest(db_path: str, wal_path: str,
                   tables: tuple[TrackedTable, ...], note: str = "") -> CollectionManifest:
    """Hash the actual inputs and assemble a manifest (used by collection tooling)."""
    return CollectionManifest(
        _fingerprint(db_path), _fingerprint(wal_path), tables, note
    )
