"""SQLite catalog schema and typed access for one registered package.

Each package directory carries one immutable ``catalog.sqlite`` describing the
package metadata, its entry points, the OASIS rewrite rules, every member with
its digest, and the resolution batches (closure files + dependency edges)
produced for requested entry sets. Writes happen inside a single SQLite
transaction on the staging copy; reads on a published catalog always open the
file read-only and never create WAL or journal side-car files.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

from .errors import RegistryCorruptionError
from .models import (
    CatalogRule,
    EntryPointSpec,
    ResolutionBatch,
    ResolutionEdge,
    ResolutionFile,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE entries (
    entry_seq        INTEGER PRIMARY KEY,
    href             TEXT NOT NULL,
    effective_base   TEXT,
    line             INTEGER NOT NULL,
    member_path      TEXT NOT NULL,
    logical_uri      TEXT NOT NULL
);
CREATE TABLE catalog_rules (
    rule_seq           INTEGER PRIMARY KEY,
    uri_start_string   TEXT NOT NULL,
    rewrite_prefix     TEXT NOT NULL,
    rewrite_prefix_raw TEXT NOT NULL,
    line               INTEGER NOT NULL
);
CREATE TABLE members (
    member_path  TEXT PRIMARY KEY,
    blob_digest  TEXT NOT NULL,
    byte_size    INTEGER NOT NULL,
    is_xml       INTEGER NOT NULL
);
CREATE TABLE batches (
    batch_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_set_hash TEXT NOT NULL UNIQUE,
    entry_uris     TEXT NOT NULL,
    file_count     INTEGER NOT NULL,
    edge_count     INTEGER NOT NULL,
    cycle_edge_count INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE batch_entries (
    batch_id      INTEGER NOT NULL REFERENCES batches(batch_id),
    entry_seq     INTEGER NOT NULL,
    logical_uri   TEXT NOT NULL,
    member_path   TEXT NOT NULL,
    PRIMARY KEY (batch_id, entry_seq)
);
CREATE TABLE resolution_files (
    batch_id     INTEGER NOT NULL REFERENCES batches(batch_id),
    public_uri   TEXT NOT NULL,
    member_path  TEXT NOT NULL,
    member_digest TEXT NOT NULL,
    byte_size    INTEGER NOT NULL,
    is_entry     INTEGER NOT NULL,
    is_xml       INTEGER NOT NULL,
    PRIMARY KEY (batch_id, public_uri),
    UNIQUE (batch_id, member_path, public_uri)
);
CREATE TABLE resolution_edges (
    batch_id     INTEGER NOT NULL REFERENCES batches(batch_id),
    source_uri   TEXT NOT NULL,
    target_uri   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    raw_href     TEXT NOT NULL,
    is_cycle     INTEGER NOT NULL,
    PRIMARY KEY (batch_id, source_uri, target_uri, kind)
);
CREATE INDEX idx_rules_prefix ON catalog_rules(uri_start_string);
CREATE INDEX idx_files_member ON resolution_files(batch_id, member_path);
"""


class PackageCatalog:
    """Typed wrapper around one package's catalog.sqlite."""

    def __init__(self, db_path: str, *, writable: bool,
                 create: bool = False):
        self.db_path = db_path
        self._writable = writable
        if writable:
            self._conn = sqlite3.connect(db_path, isolation_level=None)
        else:
            self._conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if writable:
            # Staging databases are renamed over the published catalog; they
            # must not leave WAL or journal side cars behind.
            self._conn.execute("PRAGMA journal_mode = DELETE")
        if create:
            if not writable:
                raise ValueError("create=True requires a writable connection")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
        else:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or row["value"] != str(SCHEMA_VERSION):
                raise RegistryCorruptionError(
                    f"catalog {db_path} has unsupported schema version")

    def close(self) -> None:
        path = self.db_path
        self._conn.close()
        if self._writable:
            # With journal_mode=DELETE the journal is gone after commit;
            # remove any stragglers so the staging directory holds one file.
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

    def __enter__(self) -> "PackageCatalog":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- registration writes ---------------------------------------------------

    def write_meta_fields(self, fields: dict[str, str]) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            sorted(fields.items()))

    def write_members(self, members) -> None:
        self._conn.executemany(
            "INSERT INTO members (member_path, blob_digest, byte_size, is_xml)"
            " VALUES (?,?,?,?)",
            [(m.path, m.sha256, m.byte_size, 1 if m.is_xml else 0)
             for m in members])

    def write_rules(self, rules: tuple[CatalogRule, ...]) -> None:
        self._conn.executemany(
            "INSERT INTO catalog_rules (rule_seq, uri_start_string,"
            " rewrite_prefix, rewrite_prefix_raw, line) VALUES (?,?,?,?,?)",
            [(i, r.uri_start_string, r.rewrite_prefix, r.rewrite_prefix_raw,
              r.line) for i, r in enumerate(rules)])

    def write_entries(self, specs: tuple[EntryPointSpec, ...],
                      member_paths: tuple[str, ...],
                      logical_uris: tuple[str, ...]) -> None:
        self._conn.executemany(
            "INSERT INTO entries (entry_seq, href, effective_base, line,"
            " member_path, logical_uri) VALUES (?,?,?,?,?,?)",
            [(i, s.href, s.effective_base, s.line, member_paths[i],
              logical_uris[i]) for i, s in enumerate(specs)])

    # -- resolution writes ------------------------------------------------------

    def find_batch(self, entry_set_hash: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM batches WHERE entry_set_hash = ?",
            (entry_set_hash,)).fetchone()

    def insert_batch(self, *, entry_set_hash: str, entry_uris: tuple[str, ...],
                     files: tuple[ResolutionFile, ...],
                     edges: tuple[ResolutionEdge, ...],
                     created_at: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO batches (entry_set_hash, entry_uris, file_count,"
            " edge_count, cycle_edge_count, created_at) VALUES (?,?,?,?,?,?)",
            (entry_set_hash, "\n".join(entry_uris), len(files), len(edges),
             sum(1 for e in edges if e.is_cycle), created_at))
        batch_id = cur.lastrowid
        self._conn.executemany(
            "INSERT INTO batch_entries (batch_id, entry_seq, logical_uri,"
            " member_path) VALUES (?,?,?,?)",
            [(batch_id, i, uri,
              next(f.member_path for f in files if f.public_uri == uri))
             for i, uri in enumerate(entry_uris)])
        self._conn.executemany(
            "INSERT INTO resolution_files (batch_id, public_uri, member_path,"
            " member_digest, byte_size, is_entry, is_xml)"
            " VALUES (?,?,?,?,?,?,?)",
            [(batch_id, f.public_uri, f.member_path, f.member_sha256,
              f.byte_size, 1 if f.is_entry else 0, 1 if f.is_xml else 0)
             for f in files])
        self._conn.executemany(
            "INSERT INTO resolution_edges (batch_id, source_uri, target_uri,"
            " kind, raw_href, is_cycle) VALUES (?,?,?,?,?,?)",
            [(batch_id, e.source_uri, e.target_uri, e.kind, e.raw_href,
              1 if e.is_cycle else 0) for e in edges])
        return batch_id

    # -- reads ------------------------------------------------------------------

    def meta_fields(self) -> dict[str, str]:
        return {row["key"]: row["value"]
                for row in self._conn.execute("SELECT key, value FROM meta")}

    def member_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM members ORDER BY member_path"))

    def rule_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM catalog_rules ORDER BY rule_seq"))

    def entry_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM entries ORDER BY entry_seq"))

    def batch_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM batches ORDER BY batch_id"))

    def find_member(self, member_path: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM members WHERE member_path = ?",
            (member_path,)).fetchone()

    def find_resolution_files(self, uri: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM resolution_files WHERE public_uri = ?"
            " OR member_path = ? ORDER BY batch_id, public_uri",
            (uri, uri)))

    def find_resolution_files_via_edges(self, uri: str) -> list[sqlite3.Row]:
        """Files a URI reaches as the target of a matching raw/edge reference."""
        return list(self._conn.execute(
            "SELECT DISTINCT f.* FROM resolution_files f"
            " JOIN resolution_edges e"
            " ON e.batch_id = f.batch_id AND e.target_uri = f.public_uri"
            " WHERE e.raw_href = ? OR e.source_uri = ? OR e.target_uri = ?"
            " ORDER BY f.batch_id, f.public_uri",
            (uri, uri, uri)))

    def find_edges(self, uri: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM resolution_edges WHERE raw_href = ?"
            " OR source_uri = ? OR target_uri = ?"
            " ORDER BY batch_id, source_uri, target_uri",
            (uri, uri, uri)))

    def load_batch(self, batch_id: int) -> ResolutionBatch:
        batch = self._conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if batch is None:
            raise RegistryCorruptionError(
                f"batch {batch_id} missing from catalog {self.db_path}")
        entry_rows = self._conn.execute(
            "SELECT * FROM batch_entries WHERE batch_id = ? ORDER BY entry_seq",
            (batch_id,)).fetchall()
        file_rows = self._conn.execute(
            "SELECT * FROM resolution_files WHERE batch_id = ?"
            " ORDER BY public_uri", (batch_id,)).fetchall()
        edge_rows = self._conn.execute(
            "SELECT * FROM resolution_edges WHERE batch_id = ?"
            " ORDER BY source_uri, target_uri, kind",
            (batch_id,)).fetchall()
        return ResolutionBatch(
            batch_id=batch_id,
            entry_uris=tuple(r["logical_uri"] for r in entry_rows),
            entry_member_paths=tuple(r["member_path"] for r in entry_rows),
            files=tuple(ResolutionFile(
                public_uri=r["public_uri"], member_path=r["member_path"],
                member_sha256=r["member_digest"], byte_size=r["byte_size"],
                is_entry=bool(r["is_entry"]), is_xml=bool(r["is_xml"]))
                for r in file_rows),
            edges=tuple(ResolutionEdge(
                source_uri=r["source_uri"], target_uri=r["target_uri"],
                kind=r["kind"], raw_href=r["raw_href"],
                is_cycle=bool(r["is_cycle"])) for r in edge_rows),
            file_count=batch["file_count"],
            edge_count=batch["edge_count"],
            cycle_edge_count=batch["cycle_edge_count"],
        )
