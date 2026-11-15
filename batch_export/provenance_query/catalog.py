"""Base-table catalogue: metadata and explicit-primary-key validation."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

import sqlglot
from sqlglot import exp

from .cols import Collation, OutColumn, affinity_for_declared_type
from .errors import BuildError, UnsupportedQueryError


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class BaseTable:
    name: str
    pk_columns: tuple[str, ...]
    columns: list[OutColumn]
    column_names: list[str]

    def index_of(self, name: str) -> Optional[int]:
        try:
            return self.column_names.index(name)
        except ValueError:
            return None


_IDENT = re.compile(r"([A-Za-z_][A-Za-z0-9_$]*)")


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    quote_char: Optional[str] = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote_char is not None:
            cur.append(ch)
            if ch == quote_char:
                if i + 1 < len(text) and text[i + 1] == quote_char:
                    cur.append(text[i + 1])
                    i += 2
                    continue
                quote_char = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote_char = ch
            cur.append(ch)
            i += 1
            continue
        if ch == "[":
            quote_char = "]"
            cur.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p for p in parts if p.strip()]


def _leading_identifier(text: str) -> tuple[Optional[str], str]:
    s = text.lstrip()
    if not s:
        return None, ""
    if s[0] in ('"', "'", "`"):
        qc = s[0]
        out: list[str] = []
        i = 1
        while i < len(s):
            if s[i] == qc:
                if i + 1 < len(s) and s[i + 1] == qc:
                    out.append(qc)
                    i += 2
                    continue
                return "".join(out), s[i + 1:].lstrip()
            out.append(s[i])
            i += 1
        return None, ""
    if s[0] == "[":
        end = s.find("]")
        if end < 0:
            return None, ""
        return s[1:end], s[end + 1:].lstrip()
    m = _IDENT.match(s)
    if not m:
        return None, ""
    return m.group(1), s[m.end():].lstrip()


def parse_column_definitions(ddl: str) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """{column: (declared type text, collation)} from CREATE TABLE DDL.

    The declared type text is read verbatim from SQLite's stored DDL.  We do
    not use sqlglot's rendering here: its SQLite generator normalises
    ``NUMERIC`` to ``REAL`` and applies other mappings, which would change the
    affinity the table actually has.
    """
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    try:
        start = ddl.index("(")
        end = ddl.rindex(")")
    except ValueError:
        return out
    for chunk in _split_top_level(ddl[start + 1:end]):
        stripped = chunk.strip()
        upper = stripped.upper()
        if upper.startswith(("PRIMARY ", "UNIQUE", "CHECK", "FOREIGN ", "CONSTRAINT ")):
            continue
        name, rest = _leading_identifier(stripped)
        if name is None:
            continue
        declared = _leading_type_text(rest)
        coll = None
        cm = re.search(r"\bCOLLATE\s+([A-Za-z_][A-Za-z0-9_]*)", rest, re.I)
        if cm:
            coll = cm.group(1)
        out[name] = (declared, coll)
    return out


_TYPE_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)")
_TYPE_PARAMS = re.compile(r"\s*(\([^)]*\))?")


def _leading_type_text(rest: str) -> Optional[str]:
    """The declared type as written, stopping before a constraint keyword."""
    text = rest.strip()
    if not text:
        return None
    if text.upper().startswith(("PRIMARY", "NOT", "NULL", "DEFAULT", "COLLATE",
                                "UNIQUE", "CHECK", "REFERENCES", "GENERATED", "AS")):
        return None
    m = _TYPE_NAME.match(text)
    if not m:
        return None
    end = m.end()
    params = _TYPE_PARAMS.match(text, end)
    if params is not None:
        end = params.end() or end
    return text[:end].strip() or None


class Catalog:
    """Read-only metadata and identity validation for base tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._tables: dict[str, BaseTable] = {}

    def table(self, name: str) -> BaseTable:
        # SQLite resolves table names case-insensitively; cache by the folded
        # name so two spellings share one canonical BaseTable.
        cache_key = "".join(
            ch.lower() if "A" <= ch <= "Z" else ch for ch in name
        )
        cached = self._tables.get(cache_key)
        if cached is not None:
            return cached
        row = self.conn.execute(
            "SELECT name, type, sql FROM sqlite_master"
            " WHERE name = ? COLLATE NOCASE AND type IN ('table', 'view')"
            " ORDER BY (name = ? COLLATE BINARY) DESC LIMIT 1",
            (name, name,)
        ).fetchone()
        if row is None:
            raise BuildError(f"base table {name!r} does not exist in the source database")
        stored_name, object_type, ddl_source = row
        if object_type != "table":
            raise UnsupportedQueryError(
                f"{name!r} is a {object_type}, not a plain table"
            )
        ddl = ddl_source or ""
        if ddl.upper().startswith("CREATE VIRTUAL TABLE"):
            raise UnsupportedQueryError(f"{name!r} is a virtual table")
        info = list(self.conn.execute(f"PRAGMA table_info({quote(name)})").fetchall())
        xinfo = list(self.conn.execute(f"PRAGMA table_xinfo({quote(name)})").fetchall())
        if not info:
            raise BuildError(f"base table {name!r} has no readable columns")
        declarations = parse_column_definitions(ddl) if ddl else {}
        pk_columns = tuple(r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] > 0)
        if not pk_columns:
            raise BuildError(
                f"base table {name!r} has no explicit PRIMARY KEY; the hidden rowid"
                " is not accepted as a durable row identity"
            )
        columns: list[OutColumn] = []
        # table_info hides generated columns; table_xinfo includes them (hidden
        # flag 2 = VIRTUAL, 3 = STORED).  ``SELECT *`` includes generated
        # columns, so they must stay visible and in declared order.
        for entry in xinfo:
            cid, colname, decl, _notnull, _dflt, _pkpos = entry[:6]
            hidden = entry[6] if len(entry) > 6 else 0
            if hidden not in (0, 2, 3):
                continue  # hidden columns of virtual tables are not readable
            stored_decl, coll = declarations.get(colname, (decl, None))
            if stored_decl is None:
                stored_decl = decl
            try:
                collation = Collation.from_sql(coll)
            except ValueError:
                raise UnsupportedQueryError(
                    f"base table {name!r}: column {colname!r} uses an unsupported collation"
                ) from None
            columns.append(
                OutColumn(
                    name=colname,
                    affinity=affinity_for_declared_type(stored_decl or ""),
                    collation=collation,
                    base_table=name,
                    base_column=colname,
                )
            )
        # The canonical name is the spelling stored in sqlite_master, so the
        # token identity is stable regardless of how the query spells it.
        table = BaseTable(
            stored_name, pk_columns, columns, [c.name for c in columns]
        )
        _validate_identity(self.conn, table, info)
        self._tables[cache_key] = table
        return table


def _is_rowid_alias(pk_columns: Sequence[str], info) -> bool:
    if len(pk_columns) != 1:
        return False
    for _cid, colname, decl, _notnull, _dflt, _pkpos in info:
        if colname == pk_columns[0]:
            return (decl or "").strip().upper() == "INTEGER"
    return False


def _unique_pk_index(conn, table: str, pk_columns: Sequence[str], info) -> bool:
    if _is_rowid_alias(pk_columns, info):
        return True  # INTEGER PRIMARY KEY is the rowid; uniqueness is structural
    for idx in conn.execute(f"PRAGMA index_list({quote(table)})").fetchall():
        if len(idx) < 3 or not idx[2]:
            continue
        if len(idx) >= 5 and idx[4]:
            continue  # partial index
        cols = [
            r[2]
            for r in conn.execute(f"PRAGMA index_info({quote(idx[1])})").fetchall()
            if r[2] is not None
        ]
        if cols == list(pk_columns):
            return True
    return False


def _validate_identity(conn, table: BaseTable, info) -> None:
    if not _is_rowid_alias(table.pk_columns, info):
        probe = conn.execute(
            "SELECT 1 FROM " + quote(table.name) + " WHERE "
            + " OR ".join(f"({quote(c)} IS NULL)" for c in table.pk_columns)
            + " LIMIT 1"
        ).fetchone()
        if probe is not None:
            raise BuildError(
                f"base table {table.name!r}: primary key column(s)"
                f" {list(table.pk_columns)} contain NULL; a row identity must be"
                " complete and non-null"
            )
    duplicate = conn.execute(
        "SELECT 1 FROM (SELECT 1 FROM " + quote(table.name) + " GROUP BY "
        + ", ".join(quote(c) for c in table.pk_columns)
        + " HAVING COUNT(*) > 1) LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        raise BuildError(
            f"base table {table.name!r}: its declared PRIMARY KEY is not unique in"
            " the stored rows"
        )
    if not _unique_pk_index(conn, table.name, table.pk_columns, info):
        raise BuildError(
            f"base table {table.name!r}: no UNIQUE index backs its PRIMARY KEY"
        )
