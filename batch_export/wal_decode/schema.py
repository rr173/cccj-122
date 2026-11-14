"""Read-only SQLite image inspection and schema compatibility checks.

All SQLite access in this package goes through ``immutable=1`` read-only URIs
on *copies* inside the tool's own temporary directory, or on the pristine
baseline for read-only inspection. The original database and WAL are never
opened together, so no automatic recovery, checkpoint or ``-shm`` sidecar is
ever triggered on the inputs.

Only current SQLite 3, unencrypted, UTF-8 databases with ordinary (non-virtual)
tables are accepted. Virtual tables, views and attached databases are
diagnosed before any business row is read. Schema fingerprints compare
user objects by name/type/SQL text (root page numbers are excluded on
purpose: a VACUUM legitimately moves pages without changing the structure).
"""
from __future__ import annotations

import os
import sqlite3
import struct
from dataclasses import dataclass

from .errors import InputValidationError, WalUnsupportedError
from .manifest import TrackedTable, quote_identifier

SQLITE_FILE_MAGIC = b"SQLite format 3\x00"
# Header offsets from the SQLite file format documentation
HDR_PAGE_SIZE = 16
HDR_WRITE_VERSION = 18
HDR_READ_VERSION = 19
HDR_TEXT_ENCODING = 56
HDR_TEXT_ENCODING_LEN = 4
ENCODING_UTF8 = 1


def read_database_header(path: str) -> dict:
    """Validate the 100-byte database header without opening SQLite."""
    with open(path, "rb") as fh:
        header = fh.read(100)
    if len(header) < 100:
        raise InputValidationError(
            f"{path}: not a SQLite database (file shorter than the 100-byte header)"
        )
    if header[:16] != SQLITE_FILE_MAGIC:
        raise WalUnsupportedError(
            f"{path}: missing 'SQLite format 3' magic; encrypted or non-SQLite files "
            "are not supported",
            offset=0,
        )
    raw_page_size = struct.unpack(">H", header[HDR_PAGE_SIZE:HDR_PAGE_SIZE + 2])[0]
    page_size = 65536 if raw_page_size == 1 else raw_page_size
    if page_size < 512 or (page_size & (page_size - 1)):
        raise WalUnsupportedError(
            f"{path}: invalid database page size {page_size} in header",
            offset=HDR_PAGE_SIZE,
        )
    write_version, read_version = header[HDR_WRITE_VERSION], header[HDR_READ_VERSION]
    if write_version not in (1, 2) or read_version not in (1, 2):
        raise WalUnsupportedError(
            f"{path}: unsupported journal mode versions "
            f"(read={read_version}, write={write_version}); only rollback(1)/WAL(2) "
            "current SQLite files are accepted",
            offset=HDR_READ_VERSION,
        )
    encoding = struct.unpack(">I", header[HDR_TEXT_ENCODING:HDR_TEXT_ENCODING_LEN + 56])[0]
    if encoding != ENCODING_UTF8:
        raise WalUnsupportedError(
            f"{path}: database text encoding is {encoding} (1=UTF-8, 2=UTF-16le, "
            "3=UTF-16be); only UTF-8 is supported",
            offset=HDR_TEXT_ENCODING,
        )
    return {"page_size": page_size, "write_version": write_version,
            "read_version": read_version, "text_encoding": encoding}


def open_immutable(path: str) -> sqlite3.Connection:
    """Open a database file strictly read-only with no sidecar files."""
    uri = "file:" + path.replace("?", "%3f").replace("#", "%23") + "?immutable=1"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    # Defence in depth: no attached databases, no writable pragma effects.
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class ColumnInfo:
    cid: int
    name: str
    declared_type: str | None
    notnull: int
    pk_position: int          # 0 = not part of the table PRIMARY KEY
    generated: bool


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[ColumnInfo, ...]
    sql_text: str

    def column(self, name: str) -> ColumnInfo | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None


def _list_user_objects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()


def schema_fingerprint(conn: sqlite3.Connection) -> tuple:
    """Structure identity excluding root pages (VACUUM-safe)."""
    return tuple(
        (row["type"], row["name"], row["tbl_name"],
         (row["sql"] or "").strip())
        for row in _list_user_objects(conn)
    )


def reject_unsupported_objects(conn: sqlite3.Connection) -> None:
    """Fail early -- before any business row read -- on views/virtual tables."""
    problems: list[str] = []
    for row in _list_user_objects(conn):
        kind, name, sql = row["type"], row["name"], row["sql"] or ""
        if kind == "view":
            problems.append(f"{name!r} is a view; only ordinary tables are tracked")
        elif kind == "table":
            if sql.upper().lstrip().startswith("CREATE VIRTUAL TABLE"):
                problems.append(
                    f"{name!r} is a virtual table ({sql[:60].strip()}...); virtual "
                    "tables are not supported"
                )
    # Attached databases cannot be queried through immutable single-file opens,
    # but make the boundary explicit.
    attached = conn.execute("PRAGMA database_list").fetchall()
    if len(attached) != 1:
        problems.append(
            "attached databases are not supported; exactly one database must be open"
        )
    if problems:
        raise WalUnsupportedError("; ".join(problems))


def load_table_schema(conn: sqlite3.Connection, table: TrackedTable) -> TableSchema:
    qname = quote_identifier(table.name)
    master = conn.execute(
        "SELECT type, sql FROM sqlite_master WHERE type='table' AND name=?",
        (table.name,),
    ).fetchone()
    if master is None:
        raise InputValidationError(
            f"tracked table {table.name!r} does not exist in the baseline database"
        )
    if (master["sql"] or "").upper().lstrip().startswith("CREATE VIRTUAL TABLE"):
        raise WalUnsupportedError(
            f"tracked table {table.name!r} is a virtual table; not supported"
        )
    columns = load_table_columns(conn, table.name)
    schema = TableSchema(table.name, columns, (master["sql"] or "").strip())
    _validate_identity(conn, table, schema)
    return schema


def _validate_identity(conn: sqlite3.Connection, table: TrackedTable,
                       schema: TableSchema) -> None:
    for column_name in table.identity_columns:
        col = schema.column(column_name)
        if col is None:
            raise InputValidationError(
                f"identity column {column_name!r} does not exist in table "
                f"{table.name!r}; columns are "
                f"{[c.name for c in schema.columns]}"
            )
        if col.generated:
            raise InputValidationError(
                f"identity column {column_name!r} of {table.name!r} is generated; "
                "generated columns cannot identify stored rows"
            )
        if not col.notnull and col.pk_position == 0:
            raise InputValidationError(
                f"identity column {column_name!r} of {table.name!r} is not declared "
                "NOT NULL and is not part of the PRIMARY KEY"
            )
    _validate_identity_unique(conn, table)
    _validate_no_null_identity(conn, table)


def _validate_identity_unique(conn: sqlite3.Connection, table: TrackedTable) -> None:
    qname = quote_identifier(table.name)
    # PRIMARY KEY columns are reported directly by table_info (pk_position);
    # an INTEGER PRIMARY KEY is a rowid alias and has no entry in index_list.
    pk_in_order = tuple(
        col.name for col in sorted(
            (c for c in load_table_columns(conn, table.name) if c.pk_position),
            key=lambda c: c.pk_position,
        )
    )
    identity = tuple(table.identity_columns)
    if pk_in_order[: len(identity)] == identity:
        return
    # Otherwise a UNIQUE index must cover the identity columns in order.
    index_rows = conn.execute(f"PRAGMA index_list({qname})").fetchall()
    for index_row in index_rows:
        if index_row["unique"] != 1:
            continue
        index_name = index_row["name"]
        info = conn.execute(
            f"PRAGMA index_info({quote_identifier(index_name)})"
        ).fetchall()
        index_cols = tuple(r["name"] for r in info)
        if index_cols[: len(identity)] == identity:
            return
    raise InputValidationError(
        f"identity columns {list(table.identity_columns)} of table {table.name!r} "
        "are not covered by a UNIQUE index or PRIMARY KEY; row identity must be "
        "unique"
    )


def load_table_columns(conn: sqlite3.Connection, table_name: str) -> tuple[ColumnInfo, ...]:
    qname = quote_identifier(table_name)
    pragma_rows = conn.execute(f"PRAGMA table_info({qname})").fetchall()
    hidden_cids: set[int] = set()
    xinfo_rows = conn.execute(f"PRAGMA table_xinfo({qname})").fetchall()
    if xinfo_rows and "hidden" in xinfo_rows[0].keys():
        hidden_cids = {r["cid"] for r in xinfo_rows if r["hidden"] in (2, 3)}
    return tuple(
        ColumnInfo(
            cid=row["cid"], name=row["name"], declared_type=row["type"],
            notnull=row["notnull"], pk_position=row["pk"],
            generated=(row["cid"] in hidden_cids),
        )
        for row in pragma_rows
    )


def _validate_no_null_identity(conn: sqlite3.Connection, table: TrackedTable) -> None:
    qname = quote_identifier(table.name)
    null_test = " OR ".join(
        f"{quote_identifier(c)} IS NULL" for c in table.identity_columns
    )
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM {qname} WHERE {null_test}"
    ).fetchone()
    if row["c"]:
        raise InputValidationError(
            f"table {table.name!r} contains {row['c']} row(s) with NULL identity "
            "values in the baseline; identity must be non-null"
        )


def assert_compatible(conn: sqlite3.Connection, baseline_fingerprint: tuple,
                      tables: tuple[TrackedTable, ...]) -> None:
    """Raise if the structure changed between the baseline and a later image."""
    current = schema_fingerprint(conn)
    if current != baseline_fingerprint:
        before = {name: (typ, sql) for typ, name, _tbl, sql in baseline_fingerprint}
        after = {name: (typ, sql) for typ, name, _tbl, sql in current}
        details: list[str] = []
        for name in sorted(set(before) | set(after)):
            if name not in after:
                details.append(f"object {name!r} ({before[name][0]}) was dropped")
            elif name not in before:
                details.append(f"object {name!r} ({after[name][0]}) was created")
            elif before[name] != after[name]:
                details.append(f"object {name!r} changed definition")
        raise WalUnsupportedError(
            "database structure changed within the decoded WAL; continuing with stale "
            "column positions would misread values. Details: " + "; ".join(details)
        )
    # Re-resolve tracked tables so a column redefinition (which also changes the
    # SQL text above) cannot silently reuse old column ids.
    for table in tables:
        load_table_schema(conn, table)
