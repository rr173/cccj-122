"""Result-file format: schema, writer, atomic publication, reader helpers.

Layout (one SQLite database):

``result_meta``       key/value build metadata (query text, counts, ...)
``result_columns``    ordered output columns (name, affinity, collation)
``result_rows``       one row per distinct typed tuple: tuple JSON, arbitrary
                      precision multiplicity text, graph root node
``result_terms``      one row per base row used as a token: table identity,
                      primary-key column names, typed PK values JSON
``graph_nodes``       the shared provenance DAG: kind + up to two children

Multiplicities are decimal TEXT so no JSON consumer can lose precision.  The
writer builds into a temporary sibling file and ``os.replace``s it into place
only after the whole build succeeds, so a failed build never leaves a readable
half-result.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from .errors import BuildError

FORMAT_NAME = "provenance-query-result"
FORMAT_VERSION = 1

SCHEMA = """
CREATE TABLE result_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE result_columns (
    ordinal   INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    affinity  TEXT NOT NULL,
    collation TEXT NOT NULL
);
CREATE TABLE result_rows (
    ordinal      INTEGER PRIMARY KEY,
    tuple_json   TEXT NOT NULL,
    multiplicity TEXT NOT NULL,
    graph_root   INTEGER NOT NULL
);
CREATE TABLE result_terms (
    token            INTEGER PRIMARY KEY,
    table_name       TEXT NOT NULL,
    table_identity   TEXT NOT NULL,
    pk_columns       TEXT NOT NULL,
    pk_values_json   TEXT NOT NULL
);
CREATE TABLE graph_nodes (
    node INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    arg1 INTEGER,
    arg2 INTEGER
);
CREATE INDEX graph_nodes_kind ON graph_nodes(kind);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResultWriter:
    """Write a complete result file and publish it atomically."""

    def __init__(self, target_path: str):
        self.target_path = os.path.abspath(target_path)
        directory = os.path.dirname(self.target_path) or "."
        fd, self.tmp_path = tempfile.mkstemp(
            prefix=".provenance-result-", suffix=".sqlite", dir=directory
        )
        os.close(fd)
        self.conn = sqlite3.connect(self.tmp_path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode = OFF")
        self.conn.execute("PRAGMA synchronous = OFF")
        self.conn.executescript(SCHEMA)
        self._closed = False

    # -- writing ----------------------------------------------------------

    def set_meta(self, **items) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO result_meta(key, value) VALUES (?, ?)",
            [(str(k), str(v)) for k, v in items.items()],
        )

    def set_columns(self, columns) -> None:
        self.conn.executemany(
            "INSERT INTO result_columns(ordinal, name, affinity, collation)"
            " VALUES (?, ?, ?, ?)",
            [
                (i, col.name, col.affinity, col.collation.name)
                for i, col in enumerate(columns)
            ],
        )

    def add_row(self, ordinal: int, values, multiplicity: int, graph_root: int) -> None:
        from .values import encode_tuple

        payload = json.dumps(
            {"v": encode_tuple(values)}, separators=(",", ":"), ensure_ascii=False
        )
        self.conn.execute(
            "INSERT INTO result_rows(ordinal, tuple_json, multiplicity, graph_root)"
            " VALUES (?, ?, ?, ?)",
            (ordinal, payload, str(int(multiplicity)), int(graph_root)),
        )

    def add_term(self, token_info) -> None:
        payload = json.dumps(
            [value.encode() for value in token_info.pk_values],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self.conn.execute(
            "INSERT INTO result_terms(token, table_name, table_identity,"
            " pk_columns, pk_values_json) VALUES (?, ?, ?, ?, ?)",
            (
                token_info.token_id,
                token_info.table,
                token_info.table_identity,
                json.dumps(list(token_info.pk_columns), ensure_ascii=False),
                payload,
            ),
        )

    def add_graph(self, graph) -> None:
        rows = []
        for idx, (kind, args) in enumerate(zip(graph.kinds, graph.args)):
            arg1 = int(args[0]) if args else None
            arg2 = int(args[1]) if len(args) > 1 else None
            if kind in ("add", "mul", "token", "one", "zero"):
                rows.append((idx, kind, arg1, arg2))
            else:  # pragma: no cover - internal
                raise BuildError(f"unknown provenance node kind {kind!r}")
        self.conn.executemany(
            "INSERT INTO graph_nodes(node, kind, arg1, arg2) VALUES (?, ?, ?, ?)",
            rows,
        )

    # -- closing ----------------------------------------------------------

    def commit(self) -> None:
        if not self._closed:
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")
            self.conn.close()
            self._closed = True

    def publish(self) -> None:
        self.commit()
        os.replace(self.tmp_path, self.target_path)

    def abort(self) -> None:
        if not self._closed:
            try:
                self.conn.close()
            except Exception:  # pragma: no cover - defensive
                pass
            self._closed = True
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass
