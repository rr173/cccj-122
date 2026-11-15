"""Read a result file and evaluate base-row exclusions from its graph.

Both operations depend only on the result file: the source warehouse may have
been moved or deleted.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .errors import BuildError, InvocationError
from .graph import ProvenanceGraph, TokenInfo, Universe
from .resultfile import FORMAT_NAME, FORMAT_VERSION
from .values import TyValue


@dataclass(frozen=True)
class StoredRow:
    ordinal: int
    values: tuple
    multiplicity: int
    graph_root: int


@dataclass(frozen=True)
class ResultColumn:
    ordinal: int
    name: str
    affinity: str
    collation: str


@dataclass(frozen=True)
class BaseIdentity:
    token: int
    table: str
    table_identity: str
    pk_columns: tuple
    pk_values: tuple

    def describe(self) -> str:
        parts = ", ".join(
            f"{name}={value.display()}"
            for name, value in zip(self.pk_columns, self.pk_values)
        )
        return f"{self.table}({parts})"


class ExclusionError(InvocationError):
    """An exclusion request names unknown or malformed base rows."""


class ProvenanceResultReader:
    """Read-only access to one provenance result file."""

    def __init__(self, path: str):
        self.path = str(path)
        p = Path(self.path)
        if not p.is_file():
            raise InvocationError(f"result file not found: {self.path}")
        from .service import read_only_uri

        uri = read_only_uri(p)
        try:
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        except sqlite3.Error as exc:
            raise InvocationError(f"cannot open result file read-only: {exc}") from None
        self.conn.row_factory = sqlite3.Row
        self._validate()

    def __enter__(self) -> "ProvenanceResultReader":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _validate(self) -> None:
        try:
            tables = {
                row[0]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        except sqlite3.Error as exc:
            raise BuildError(f"result file is not a readable SQLite database: {exc}") from None
        required = {"result_meta", "result_columns", "result_rows",
                    "result_terms", "graph_nodes"}
        missing = required - tables
        if missing:
            raise BuildError(
                "not a provenance result file; missing table(s): "
                + ", ".join(sorted(missing))
            )
        meta = self.meta()
        if meta.get("format") != FORMAT_NAME:
            raise BuildError(f"unexpected result file format {meta.get('format')!r}")
        try:
            version = int(meta.get("format_version", "0"))
        except (TypeError, ValueError):
            version = -1
        if version != FORMAT_VERSION:
            raise BuildError(
                f"unsupported result format version {meta.get('format_version')!r}"
            )

    # -- metadata ---------------------------------------------------------

    def meta(self) -> dict:
        return dict(self.conn.execute("SELECT key, value FROM result_meta").fetchall())

    def columns(self) -> list[ResultColumn]:
        return [
            ResultColumn(r["ordinal"], r["name"], r["affinity"], r["collation"])
            for r in self.conn.execute("SELECT * FROM result_columns ORDER BY ordinal")
        ]

    def column_names(self) -> list[str]:
        return [column.name for column in self.columns()]

    def row_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM result_rows").fetchone()[0]

    # -- stored rows ------------------------------------------------------

    def rows(self) -> list[StoredRow]:
        out = []
        for r in self.conn.execute("SELECT * FROM result_rows ORDER BY ordinal"):
            payload = json.loads(r["tuple_json"])
            values = tuple(TyValue.decode(item) for item in payload["v"])
            out.append(
                StoredRow(
                    ordinal=r["ordinal"],
                    values=values,
                    multiplicity=int(r["multiplicity"]),
                    graph_root=r["graph_root"],
                )
            )
        return out

    def raw_compressed(self) -> dict:
        """The compressed result, JSON-friendly (counts stay decimal strings)."""
        return {
            "meta": self.meta(),
            "columns": [
                {
                    "ordinal": column.ordinal,
                    "name": column.name,
                    "affinity": column.affinity,
                    "collation": column.collation,
                }
                for column in self.columns()
            ],
            "rows": [
                {
                    "ordinal": row.ordinal,
                    "values": [value.encode() for value in row.values],
                    "multiplicity": str(row.multiplicity),
                    "graph_root": row.graph_root,
                }
                for row in self.rows()
            ],
        }

    # -- base identities and graph ---------------------------------------

    def base_identities(self) -> list[BaseIdentity]:
        out = []
        for r in self.conn.execute("SELECT * FROM result_terms ORDER BY token"):
            values = tuple(
                TyValue.decode(v) for v in json.loads(r["pk_values_json"])
            )
            out.append(
                BaseIdentity(
                    token=r["token"],
                    table=r["table_name"],
                    table_identity=r["table_identity"],
                    pk_columns=tuple(json.loads(r["pk_columns"])),
                    pk_values=values,
                )
            )
        return out

    def universe(self) -> Universe:
        universe = Universe()
        for r in self.conn.execute("SELECT * FROM result_terms ORDER BY token"):
            values = [TyValue.decode(v) for v in json.loads(r["pk_values_json"])]
            info = TokenInfo(
                r["token"],
                r["table_name"],
                r["table_identity"],
                json.loads(r["pk_columns"]),
                values,
            )
            if info.token_id != len(universe.tokens):
                raise BuildError(
                    f"result file has non-contiguous token ids near {info.token_id}"
                )
            universe.tokens.append(info)
            universe._by_key["\x1f".join(
                [info.table_identity, *[v.key() for v in values]]
            )] = info.token_id
            universe.tables[info.table_identity] = {
                "table_identity": info.table_identity,
                "table": info.table,
                "pk_columns": list(info.pk_columns),
                "row_count": 0,
            }
        return universe

    def graph(self) -> ProvenanceGraph:
        universe = self.universe()
        graph = ProvenanceGraph(universe)
        graph.kinds = []
        graph.args = []
        graph._intern = {}
        for r in self.conn.execute("SELECT * FROM graph_nodes ORDER BY node"):
            kind = r["kind"]
            if kind in ("add", "mul"):
                args = (int(r["arg1"]), int(r["arg2"]))
            elif kind == "token":
                args = (int(r["arg1"]),)
            elif kind in ("one", "zero"):
                args = ()
            else:
                raise BuildError(f"unknown provenance node kind {kind!r}")
            if r["node"] != len(graph.kinds):
                raise BuildError(
                    f"result file has non-contiguous graph nodes near {r['node']}"
                )
            graph.kinds.append(kind)
            graph.args.append(args)
            graph._intern[(kind, args)] = r["node"]
        graph._one = graph.kinds.index("one")
        graph._zero = graph.kinds.index("zero")
        return graph

    def validate(self) -> list[str]:
        """Check stored multiplicities against the graph expression."""
        problems: list[str] = []
        graph = self.graph()
        roots = []
        stored = self.rows()
        for row in stored:
            roots.append(row.graph_root)
        evaluated = graph.evaluate_many(roots)
        for row in stored:
            actual = evaluated[row.graph_root]
            if actual != row.multiplicity:
                problems.append(
                    f"row {row.ordinal}: stored multiplicity {row.multiplicity}"
                    f" but its graph evaluates to {actual}"
                )
        return problems


# ---------------------------------------------------------------------------
# Base-row identity parsing and exclusion evaluation
# ---------------------------------------------------------------------------


def _parse_scalar(text: str) -> TyValue:
    """Parse one identity value.

    Identities are written with explicit typed literals so NULL, integers,
    reals, text and blobs remain distinguishable:

    * ``NULL``
    * an integer literal (``42``, ``-7``)
    * a real literal (``1.5``, ``1e3``)
    * a single-quoted text literal (``'a b'``, with ``''`` escapes)
    * a blob literal (``X'00ff'``)
    """
    raw = text.strip()
    if not raw:
        raise ExclusionError("empty value in base-row identity")
    upper = raw.upper()
    if upper == "NULL":
        return TyValue.null()
    if len(raw) >= 3 and upper[0] == "X" and raw[1] == "'" and raw[-1] == "'":
        hex_part = raw[2:-1]
        try:
            return TyValue.blob(bytes.fromhex(hex_part))
        except ValueError:
            raise ExclusionError(f"invalid blob literal {raw!r}") from None
    if raw[0] == "'":
        if len(raw) < 2 or raw[-1] != "'":
            raise ExclusionError(f"unterminated text literal {raw!r}")
        body = raw[1:-1]
        if "''" in body:
            body = body.replace("''", "'")
        if "'" in body:
            raise ExclusionError(f"invalid text literal {raw!r}")
        return TyValue.text(body)
    try:
        return TyValue.integer(int(raw, 10))
    except ValueError:
        pass
    try:
        value = float(raw)
        if value != value and "nan" not in raw.lower():  # pragma: no cover
            raise ValueError
        return TyValue.real(value)
    except ValueError:
        raise ExclusionError(
            f"value {raw!r} is not a typed literal; use 42, 1.5, 'text',"
            " X'blobhex' or NULL"
        ) from None


def _split_identity_assignments(text: str) -> list[str]:
    """Split ``pk=value`` assignments on top-level commas (quotes respected)."""
    parts: list[str] = []
    cur: list[str] = []
    in_quote: Optional[str] = None
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quote is not None:
            cur.append(ch)
            if ch == in_quote:
                if i + 1 < len(text) and text[i + 1] == in_quote:
                    cur.append(text[i + 1])
                    i += 2
                    continue
                in_quote = None
            i += 1
            continue
        if ch == "'":
            in_quote = "'"
            cur.append(ch)
            i += 1
            continue
        if ch == ",":
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def parse_identity(text: str) -> tuple[str, list[tuple[str, TyValue]]]:
    """Parse ``TABLE(col=value, ...)`` into (table, ordered assignments)."""
    s = text.strip()
    if not s.endswith(")"):
        raise ExclusionError(
            f"base-row identity {text!r} must look like TABLE(col=value, ...)"
        )
    open_paren = s.find("(")
    if open_paren <= 0:
        raise ExclusionError(
            f"base-row identity {text!r} must look like TABLE(col=value, ...)"
        )
    table = s[:open_paren].strip()
    inner = s[open_paren + 1:-1]
    if not table or not inner.strip():
        raise ExclusionError(f"base-row identity {text!r} has an empty part")
    assignments = []
    for piece in _split_identity_assignments(inner):
        if not piece.strip():
            raise ExclusionError(f"base-row identity {text!r} has an empty assignment")
        if "=" not in piece:
            raise ExclusionError(
                f"base-row identity {text!r}: {piece!r} is not col=value"
            )
        col, value = piece.split("=", 1)
        assignments.append((col.strip(), _parse_scalar(value)))
    return table, assignments


def evaluate_exclusion(
    result_path: str,
    identities: Iterable[str],
    *,
    reader: Optional[ProvenanceResultReader] = None,
) -> dict:
    """Exclude named base rows and return the re-evaluated multiplicities.

    ``identities`` are ``TABLE(col=value, ...)`` strings naming base rows of
    *this build's input snapshot*.  Duplicates exclude once.  An identity that
    is not a token of the snapshot raises :class:`ExclusionError` -- it is
    never silently treated as already excluded.  The result file is not
    modified.
    """
    own = reader is None
    read = reader or ProvenanceResultReader(result_path)
    try:
        by_key = {}
        for identity in read.base_identities():
            key = (identity.table_identity, tuple(v.key() for v in identity.pk_values))
            by_key[key] = identity
        # Also accept the plain table name as written by the user.
        by_name: dict[tuple, list[BaseIdentity]] = {}
        for identity in by_key.values():
            by_name.setdefault(
                (identity.table, tuple(v.key() for v in identity.pk_values)), []
            ).append(identity)

        excluded_tokens: set[int] = set()
        normalized: list[str] = []
        for raw in identities:
            table, assignments = parse_identity(raw)
            key_values = tuple(v.key() for v in [value for _name, value in assignments])
            # Match by (table, typed PK values in declared column order).  The
            # declared order is known from the stored token's pk_columns; try
            # to find a token whose column names match the request's names as
            # written, in any order, then validate values in token order.
            match = _match_identity(read, table, assignments)
            if match is None:
                raise ExclusionError(
                    f"unknown base-row identity {raw!r}: it is not present in"
                    " this result's input snapshot"
                )
            excluded_tokens.add(match.token)
            normalized.append(match.describe())

        graph = read.graph()
        before = []
        after = []
        total_before = 0
        total_after = 0
        changed = 0
        removed = 0
        for row in read.rows():
            old = row.multiplicity
            new = graph.evaluate(row.graph_root, frozenset(excluded_tokens))
            total_before += old
            total_after += new
            if new != old:
                changed += 1
            if new == 0 and old != 0:
                removed += 1
            before.append(old)
            after.append(new)
        return {
            "result_path": str(result_path),
            "excluded": sorted(set(normalized)),
            "excluded_count": len(excluded_tokens),
            "rows": [
                {
                    "ordinal": row.ordinal,
                    "values": [v.encode() for v in row.values],
                    "before": old,
                    "after": new,
                }
                for row, old, new in zip(read.rows(), before, after)
            ],
            "total_before": total_before,
            "total_after": total_after,
            "rows_changed": changed,
            "rows_removed": removed,
        }
    finally:
        if own:
            read.close()


def _match_identity(
    read: ProvenanceResultReader, table: str, assignments
) -> Optional[BaseIdentity]:
    """Find the token whose table and PK values match the request."""
    requested = dict(assignments)
    if len(requested) != len(assignments):
        raise ExclusionError(
            f"base-row identity for {table!r} names the same column twice"
        )
    def fold(text: str) -> str:
        return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in text)

    folded_table = fold(table)
    for identity in read.base_identities():
        if fold(identity.table) != folded_table and fold(identity.table_identity) != folded_table:
            continue
        if set(identity.pk_columns) != set(requested):
            continue
        by_column = dict(zip(identity.pk_columns, identity.pk_values))
        if all(by_column[name].key() == requested[name].key()
               for name in identity.pk_columns):
            return identity
    return None
