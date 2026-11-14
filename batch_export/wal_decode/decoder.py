"""Offline per-transaction row change decoder.

The decoder reconstructs committed row changes purely from page versions in a
raw WAL. It never asks SQLite to open the WAL, never reads the whole WAL into
memory and never stores all rows of every commit:

1. the 32-byte WAL header and every frame are verified by
   :mod:`wal_decode.wal_format`;
2. frames are grouped into transactions at real commit boundaries (a frame
   whose ``db_size_after`` field is non-zero). Repeated writes of the same
   page inside one transaction collapse to the last frame; pages committed by
   earlier transactions stay in the working image;
3. only when a whole transaction's frames are known, the pages are overlaid
   onto a private working copy of the baseline, growing or truncating the
   image exactly to the declared post-commit page count;
4. SQLite then opens that private image strictly read-only (``immutable=1``),
   structure is re-checked against the baseline, and tracked-table rows are
   read once; the previous snapshot is diffed into net row changes and then
   discarded;
5. uncommitted tail frames are never materialised.

The output is a stream of :class:`CommitChange` objects, one per committed
transaction, including transactions that touched no tracked table.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from typing import Iterator, Optional

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
    fingerprint_file,
    load_manifest,
    verify_fingerprint,
)
from .schema import (
    TableSchema,
    assert_compatible,
    load_table_schema,
    open_immutable,
    read_database_header,
    reject_unsupported_objects,
    schema_fingerprint,
)
from .values import encode_value
from .wal_format import Frame, TailInfo, WalFrameReader

# Event ordering: deletes, then inserts, then updates; identity order inside
# each group keeps the output deterministic for composite/mixed-type keys.
_EVENT_RANK = {"delete": 0, "insert": 1, "update": 2}


@dataclass(frozen=True)
class RowEvent:
    kind: str                       # "insert" | "delete" | "update"
    table: str
    identity_columns: tuple[str, ...]
    business_columns: tuple[str, ...]
    before: dict[str, dict] | None  # full row from the previous state
    after: dict[str, dict] | None   # full row from the current state

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "table": self.table,
            "identity_columns": list(self.identity_columns),
            "business_columns": list(self.business_columns),
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class TableChanges:
    table: str
    identity_columns: tuple[str, ...]
    events: tuple[RowEvent, ...]

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "identity_columns": list(self.identity_columns),
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True)
class CommitChange:
    commit_seq: int                 # 1-based ordinal among committed transactions
    commit_frame_no: int            # 0-based index of the commit frame
    first_frame_no: int             # first frame of this transaction
    last_frame_no: int              # equals commit_frame_no
    db_page_count_after: int
    changes: tuple[TableChanges, ...]
    state_before_commit_seq: int    # state number the "before" rows came from
    state_after_commit_seq: int     # state number the "after" rows came from

    def to_dict(self) -> dict:
        return {
            "commit_seq": self.commit_seq,
            "commit_frame_no": self.commit_frame_no,
            "frame_range": [self.first_frame_no, self.last_frame_no],
            "db_page_count_after": self.db_page_count_after,
            "state_before": self.state_before_commit_seq,
            "state_after": self.state_after_commit_seq,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass
class DecodeResult:
    outcome: Outcome
    commits: list[CommitChange] = field(default_factory=list)
    tail: Optional[TailInfo] = None
    error: Optional[WalDecodeError] = None

    @property
    def is_complete(self) -> bool:
        return self.outcome is Outcome.OK


class _TableReader:
    """Reads tracked table rows from one immutable image and keeps column order."""

    def __init__(self, schema: TableSchema, table: TrackedTable):
        self.schema = schema
        self.table = table
        self.identity = tuple(table.identity_columns)
        id_names = set(self.identity)
        # Business columns = stored columns in table order minus identity cols.
        self.business = tuple(c.name for c in schema.columns
                              if c.name not in id_names)
        self.all_columns = self.identity + self.business

    def read_snapshot(self, conn: sqlite3.Connection) -> dict[tuple, dict]:
        cols_sql = ", ".join('"' + c + '"' for c in self.all_columns)
        rows = conn.execute(
            f"SELECT {cols_sql} FROM "
            f'"{self.table.name}"'
        ).fetchall()
        snapshot: dict[tuple, dict] = {}
        for row in rows:
            values = list(row)
            identity = tuple(values[: len(self.identity)])
            encoded = {
                col: encode_value(value)
                for col, value in zip(self.all_columns, values)
            }
            if identity in snapshot:
                # The manifest validated uniqueness on the baseline; seeing a
                # duplicate here means the WAL broke the declared invariant.
                raise WalUnsupportedError(
                    "duplicate identity "
                    + "(" + ", ".join(encode_value(v)["type"] + ":"
                                      + str(encode_value(v).get("value"))
                                      for v in identity) + ")"
                    + f" appeared in table {self.table.name!r}; the tracked "
                    "uniqueness invariant no longer holds"
                )
            snapshot[identity] = encoded
        return snapshot


def _identity_sort_key(identity: tuple):
    """Stable ordering for composite, mixed-storage-class identities.

    Values are never string-concatenated: each component keeps its storage
    type and is compared component-wise. A type tag prefix makes ordering
    total across NULL/INTEGER/REAL/TEXT/BLOB without cross-type Python
    comparison errors.
    """
    key = []
    for value in identity:
        if value is None:
            key.append((0, 0, None))
        elif isinstance(value, bool):
            key.append((1, int(value), None))
        elif isinstance(value, int):
            key.append((1, value, None))
        elif isinstance(value, float):
            key.append((2, value, None))
        elif isinstance(value, str):
            key.append((3, value, None))
        else:  # bytes
            key.append((4, value, None))
    return tuple(key)


def _event_identity(event: RowEvent) -> tuple:
    row = event.after if event.after is not None else event.before
    return tuple(_decode_scalar(row[column]) for column in event.identity_columns)


def _decode_scalar(record: dict):
    kind = record["type"]
    if kind == "null":
        return None
    if kind == "integer":
        return int(record["value"])
    if kind == "real":
        return float(record["value"])
    if kind == "text":
        return record["value"]
    if kind == "blob":
        import base64
        return base64.b64decode(record["value"])
    raise ValueError(f"unknown encoded storage type: {kind!r}")


def _diff_snapshot(table: str, reader: _TableReader,
                   before: dict[tuple, dict],
                   after: dict[tuple, dict]) -> list[RowEvent]:
    events: list[RowEvent] = []
    before_ids = set(before)
    after_ids = set(after)
    for identity in sorted(before_ids - after_ids, key=_identity_sort_key):
        events.append(RowEvent(
            "delete", table, reader.identity, reader.business,
            before=before[identity], after=None,
        ))
    for identity in sorted(after_ids - before_ids, key=_identity_sort_key):
        events.append(RowEvent(
            "insert", table, reader.identity, reader.business,
            before=None, after=after[identity],
        ))
    for identity in sorted(before_ids & after_ids, key=_identity_sort_key):
        old_row, new_row = before[identity], after[identity]
        # Compare every declared column; an identity-column change cannot reach
        # here (it shows up as delete+insert of different identities), but the
        # full-row comparison still includes identity columns defensively.
        if old_row != new_row:
            events.append(RowEvent(
                "update", table, reader.identity, reader.business,
                before=old_row, after=new_row,
            ))
    events.sort(key=lambda ev: (_EVENT_RANK[ev.kind],
                                _identity_sort_key(_event_identity(ev))))
    return events


class WalChangeDecoder:
    """Iterate committed row changes from a baseline database plus raw WAL.

    Usage::

        with WalChangeDecoder.from_manifest("manifest.json") as decoder:
            for commit in decoder.iter_commits():
                ...          # one CommitChange per committed transaction
            print(decoder.outcome, decoder.tail)
    """

    def __init__(self, db_path: str, wal_path: str,
                 tables: tuple[TrackedTable, ...],
                 *, db_fingerprint: FileFingerprint | None = None,
                 wal_fingerprint: FileFingerprint | None = None):
        self._db_path = os.path.abspath(db_path)
        self._wal_path = os.path.abspath(wal_path)
        self._tables = tables
        self._db_fingerprint = db_fingerprint
        self._wal_fingerprint = wal_fingerprint
        self._tmpdir: str | None = None
        self._image_path: str | None = None
        self._image_fh = None
        self._baseline_conn: sqlite3.Connection | None = None
        self._image_conn: sqlite3.Connection | None = None
        self._readers: dict[str, _TableReader] = {}
        self._previous: dict[str, dict[tuple, dict]] = {}
        self._baseline_fingerprint: tuple | None = None
        self.outcome = Outcome.OK
        self.tail: TailInfo | None = None
        self.wal_header = None
        self.commit_count = 0
        self._closed = False

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_manifest(cls, manifest_path: str,
                      *, db_path: str | None = None,
                      wal_path: str | None = None) -> "WalChangeDecoder":
        manifest = load_manifest(manifest_path)
        return cls._from_manifest(manifest, db_path, wal_path)

    @classmethod
    def _from_manifest(cls, manifest: CollectionManifest,
                       db_path: str | None, wal_path: str | None) -> "WalChangeDecoder":
        actual_db = fingerprint_file(db_path or manifest.database.path)
        actual_wal = fingerprint_file(wal_path or manifest.wal.path)
        verify_fingerprint(actual_db, manifest.database, "database")
        verify_fingerprint(actual_wal, manifest.wal, "WAL")
        return cls(
            actual_db.path, actual_wal.path, manifest.tables,
            db_fingerprint=actual_db, wal_fingerprint=actual_wal,
        )

    def __enter__(self) -> "WalChangeDecoder":
        self._open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- setup ----------------------------------------------------------------

    def _reject_sidecars(self) -> None:
        """The tool never uses or creates -shm; refuse inputs that rely on one."""
        shm_path = self._db_path + "-shm"
        if os.path.exists(shm_path):
            raise InputValidationError(
                f"refusing to run while a shared-memory sidecar exists: {shm_path}. "
                "The decoder opens inputs immutably and never reads or writes -shm "
                "files; remove it and hand over the frozen baseline/WAL pair"
            )
        extra_wal = self._db_path + "-wal"
        if os.path.abspath(extra_wal) != self._wal_path and os.path.exists(extra_wal):
            raise InputValidationError(
                f"an unexpected WAL sidecar {extra_wal} sits next to the baseline and "
                f"differs from the declared WAL input {self._wal_path}"
            )

    def _open(self) -> None:
        # Fingerprints (CLI passes them explicitly when no manifest is used).
        if self._db_fingerprint is None:
            self._db_fingerprint = fingerprint_file(self._db_path)
        if self._wal_fingerprint is None:
            self._wal_fingerprint = fingerprint_file(self._wal_path)
        self._reject_sidecars()
        # Baseline structure validation on the pristine file, read-only.
        db_header = read_database_header(self._db_path)
        self._baseline_conn = open_immutable(self._db_path)
        reject_unsupported_objects(self._baseline_conn)
        self._baseline_fingerprint = schema_fingerprint(self._baseline_conn)
        schemas = {t.name: load_table_schema(self._baseline_conn, t)
                   for t in self._tables}
        for table in self._tables:
            self._readers[table.name] = _TableReader(schemas[table.name], table)

        # Private working image inside our own temporary directory.
        self._tmpdir = tempfile.mkdtemp(prefix="wal_decode_")
        self._image_path = os.path.join(self._tmpdir, "working_image.sqlite")
        shutil.copyfile(self._db_path, self._image_path)
        self._image_fh = open(self._image_path, "r+b")
        self.db_page_size = db_header["page_size"]

        # Baseline snapshots are state number 0 for every tracked table.
        for name, reader in self._readers.items():
            self._previous[name] = reader.read_snapshot(self._baseline_conn)

    # -- the streaming decode loop -------------------------------------------

    def iter_commits(self) -> Iterator[CommitChange]:
        if self._closed:
            raise WalDecodeError("decoder has already been closed")
        with open(self._wal_path, "rb") as wal_fh:
            reader = WalFrameReader(wal_fh)
            if reader.empty:
                # Zero-length WAL: a legal, explicit "no frames" result.
                self.wal_header = None
                return
            self.wal_header = reader.header
            if reader.header.page_size != self.db_page_size:
                raise WalFormatError(
                    f"WAL page size {reader.header.page_size} does not match the "
                    f"database page size {self.db_page_size}; the WAL does not belong "
                    "to this baseline"
                )
            tx_first_index = 0
            pending_pages: dict[int, bytes] = {}
            while True:
                frame = reader.read_frame()
                if frame is None:
                    break
                if frame.is_commit:
                    pending_pages[frame.page_no] = frame.data
                    commit = self._materialise_commit(
                        reader, tx_first_index, frame, pending_pages
                    )
                    yield commit
                    tx_first_index = frame.index + 1
                    pending_pages = {}
                else:
                    pending_pages[frame.page_no] = frame.data
            self.tail = reader.tail
            if reader.tail is not None:
                self.outcome = Outcome.INCOMPLETE

    def _materialise_commit(self, reader: WalFrameReader, first_index: int,
                            commit_frame: Frame,
                            pages: dict[int, bytes]) -> CommitChange:
        target_pages = commit_frame.db_size_after
        for page_no in pages:
            if page_no > target_pages:
                raise WalFormatError(
                    f"transaction ending at frame #{commit_frame.index} writes page "
                    f"{page_no} but declares {target_pages} page(s) after commit",
                    offset=commit_frame.header_offset,
                    frame_index=commit_frame.index,
                )
        self._apply_pages(pages, target_pages)
        self.commit_count += 1
        return self._read_commit_state(first_index, commit_frame, target_pages)

    def _apply_pages(self, pages: dict[int, bytes], target_pages: int) -> None:
        """Overlay committed pages, then grow/truncate to the declared size."""
        page_size = self.db_page_size
        for page_no, data in pages.items():
            self._image_fh.seek((page_no - 1) * page_size)
            self._image_fh.write(data)
        self._image_fh.flush()
        os.fsync(self._image_fh.fileno())
        expected_bytes = target_pages * page_size
        current_bytes = os.path.getsize(self._image_path)
        if expected_bytes > current_bytes:
            # Database growth: newly allocated pages should arrive in frames;
            # extend only if a page beyond current content is still missing.
            self._image_fh.seek(0, os.SEEK_END)
            self._image_fh.write(b"\x00" * (expected_bytes - current_bytes))
            self._image_fh.flush()
        elif expected_bytes < current_bytes:
            # Commit declared a smaller database (e.g. VACUUM): truncate now.
            os.ftruncate(self._image_fh.fileno(), expected_bytes)
        self._image_fh.flush()
        os.fsync(self._image_fh.fileno())
        # The image's own header must agree with the declared page count.
        self._image_fh.seek(28)
        size_in_header = int.from_bytes(self._image_fh.read(4), "big")
        if size_in_header != target_pages:
            raise WalFormatError(
                f"working image header database size {size_in_header} disagrees with "
                f"the WAL commit declaration {target_pages}"
            )

    def _read_commit_state(self, first_index: int, commit_frame: Frame,
                           target_pages: int) -> CommitChange:
        if self._image_conn is not None:
            self._image_conn.close()
            self._image_conn = None
        try:
            self._image_conn = open_immutable(self._image_path)
            assert_compatible(
                self._image_conn, self._baseline_fingerprint, self._tables
            )
            table_changes: list[TableChanges] = []
            new_snapshots: dict[str, dict[tuple, dict]] = {}
            for table in self._tables:
                reader = self._readers[table.name]
                snapshot = reader.read_snapshot(self._image_conn)
                events = _diff_snapshot(table.name, reader,
                                        self._previous[table.name], snapshot)
                table_changes.append(TableChanges(
                    table.name, reader.identity, tuple(events)
                ))
                new_snapshots[table.name] = snapshot
        except sqlite3.DatabaseError as exc:
            # A malformed committed image (bad B-tree page, ...) is WAL-level
            # corruption, not a usage mistake; classify it explicitly.
            raise WalFormatError(
                f"working image after commit frame #{commit_frame.index} is not a "
                f"readable SQLite database: {exc}",
                offset=commit_frame.header_offset,
                frame_index=commit_frame.index,
            ) from exc
        # Only one snapshot per table is retained: state N replaces state N-1.
        self._previous = new_snapshots
        seq = self.commit_count
        return CommitChange(
            commit_seq=seq,
            commit_frame_no=commit_frame.index,
            first_frame_no=first_index,
            last_frame_no=commit_frame.index,
            db_page_count_after=target_pages,
            changes=tuple(table_changes),
            state_before_commit_seq=seq - 1,
            state_after_commit_seq=seq,
        )

    # -- teardown -------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for conn in (self._image_conn, self._baseline_conn):
            if conn is not None:
                conn.close()
        if self._image_fh is not None:
            self._image_fh.close()
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
