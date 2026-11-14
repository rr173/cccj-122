"""Offline SQLite WAL change decoder.

Reconstruct per-transaction net row changes from a baseline main database and
a raw WAL file, using only page versions -- no SQL text, no prepared
INSERT/UPDATE/DELETE events. The original ingestion, export, calculation
check and backup entry points of the project are untouched; this package adds
one offline decoding entry point and is neither a backup store nor a recovery
command.

Public API:

* :class:`WalChangeDecoder` -- Python iterator over :class:`CommitChange`;
* :class:`CollectionManifest` / :class:`TrackedTable` -- collection metadata;
* :func:`decode_to_result` -- convenience wrapper collecting the stream;
* :class:`Outcome` and the error hierarchy.
"""
from __future__ import annotations

from .decoder import (
    CommitChange,
    RowEvent,
    TableChanges,
    WalChangeDecoder,
)
from .errors import (
    InputValidationError,
    Outcome,
    WalDecodeError,
    WalFormatError,
    WalUnsupportedError,
)
from .manifest import (
    CollectionManifest,
    FileFingerprint,
    TrackedTable,
    build_manifest,
    fingerprint_file,
    load_manifest,
)
from .values import decode_value, encode_value

__all__ = [
    "WalChangeDecoder",
    "CommitChange",
    "TableChanges",
    "RowEvent",
    "CollectionManifest",
    "FileFingerprint",
    "TrackedTable",
    "build_manifest",
    "load_manifest",
    "fingerprint_file",
    "encode_value",
    "decode_value",
    "Outcome",
    "WalDecodeError",
    "WalFormatError",
    "WalUnsupportedError",
    "InputValidationError",
    "decode_to_result",
]


def decode_to_result(db_path: str, wal_path: str,
                     tables: tuple[TrackedTable, ...]):
    """Convenience: run the full decode and return (commits, outcome, tail).

    The iterator interface in :class:`WalChangeDecoder` is the primary API;
    this helper simply drains it and reports the final outcome classification.
    """
    with WalChangeDecoder(db_path, wal_path, tables) as decoder:
        commits = list(decoder.iter_commits())
        return commits, decoder.outcome, decoder.tail
