"""Administrator-supplied relation catalog.

The catalog names the *public relations* callers may query.  Each relation
maps to one physical plain table in ``main`` (its *source*), publishes an
ordered list of fields, and declares the *owner path* that determines which
enterprise entity ``(scheme, identifier)`` each row belongs to.

The owner path is a chain of many-to-one hops.  Every hop lists the
positionally paired key columns on the current side (``left``) and on the
target table (``right``); composite keys and multi-hop chains are supported.
The last table of the chain (the source table itself when the path is empty)
carries the two TEXT entity columns.  No foreign keys need to exist in the
source database; the catalog is the whole declaration.

Validation happens in two stages:

* :func:`load_catalog` checks the JSON document structurally (shapes, key
  counts, path cycles, duplicate public names) without touching a database;
* :meth:`Catalog.validate_schema` checks every referenced object against the
  live database inside the query's read snapshot: sources and hop targets
  must be ordinary tables in ``main`` (never views or virtual tables),
  referenced columns must exist, must not be generated columns, and must use
  the BINARY collation; entity columns must have TEXT affinity; public names
  must differ from every physical table/view name.

A third check, :meth:`Catalog.check_target_keys`, runs at the same read
point and enforces the data contract: every hop's target key columns must be
non-NULL and unique across the target table **right now**.  A violation
fails the whole query before any user SQL runs; it never silently picks one
of several candidate owners.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

from provenance_query.catalog import parse_column_definitions
from provenance_query.cols import AFF_TEXT, affinity_for_declared_type

from .errors import ConfigError, InvocationError

CATALOG_VERSION = 1

# Conservative shape for declared column types that the staging layer will
# re-emit into the private database: plain type names with an optional
# (n) / (n,m) suffix.  Anything else is rejected as out of scope instead of
# being interpolated into a CREATE TABLE statement.
_DECLARED_TYPE_RE = re.compile(r"[A-Za-z0-9_ ]+(\([0-9]+(,[0-9]+)?\))?\Z")


def ascii_fold(name: str) -> str:
    """SQLite folds only ASCII letters when comparing identifiers."""
    return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in name)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class FieldConfig:
    """One published field: a public name bound to a source column."""

    name: str
    column: str


@dataclass(frozen=True)
class HopConfig:
    """One many-to-one hop: current-side keys -> target-table keys."""

    table: str
    left: tuple
    right: tuple


@dataclass(frozen=True)
class RelationConfig:
    name: str
    source: str
    fields: tuple
    hops: tuple
    entity_scheme: str
    entity_identifier: str
    summary_hidden: frozenset

    @property
    def terminal_table(self) -> str:
        """Table carrying the entity columns (last hop target, or source)."""
        return self.hops[-1].table if self.hops else self.source


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    declared_type: str
    generated: bool
    collation: str  # upper-case SQLite collation name; "BINARY" when default


@dataclass
class ResolvedRelation:
    """A relation after schema validation, ready for staging."""

    config: RelationConfig
    # public field name -> (source column, declared type) in published order
    fields: list


class Catalog:
    """The validated set of public relations."""

    def __init__(self, relations: Sequence[RelationConfig]):
        self.relations = list(relations)
        self._by_folded = {ascii_fold(r.name): r for r in self.relations}

    def relation(self, public_name: str) -> Optional[RelationConfig]:
        return self._by_folded.get(ascii_fold(public_name))

    def public_names(self) -> list:
        return [r.name for r in self.relations]

    # -- schema validation (inside the query's read snapshot) ---------------

    def validate_schema(self, conn: sqlite3.Connection) -> list:
        """Check every reference against the live schema; return resolved
        relations carrying the source columns' declared types."""
        table_columns: dict = {}

        def columns_of(table: str, where: str) -> dict:
            key = ascii_fold(table)
            if key not in table_columns:
                table_columns[key] = _read_table_columns(conn, table, where)
            return table_columns[key]

        physical_names = {
            ascii_fold(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        resolved = []
        for rel in self.relations:
            where = f"relation {rel.name!r}"
            if ascii_fold(rel.name) in physical_names:
                raise ConfigError(
                    f"{where}: public name {rel.name!r} collides with a physical"
                    " table or view in the database; public names must stay"
                    " clearly separate from physical objects"
                )
            source_cols = columns_of(rel.source, where)
            fields = []
            for fld in rel.fields:
                info = source_cols.get(ascii_fold(fld.column))
                if info is None:
                    raise ConfigError(
                        f"{where}: field {fld.name!r} references unknown column"
                        f" {fld.column!r} of table {rel.source!r}"
                    )
                _check_usable(info, f"{where} field {fld.name!r}")
                fields.append((fld, info))
            # Owner path tables and keys.
            current = source_cols
            current_table = rel.source
            for position, hop in enumerate(rel.hops, start=1):
                hop_where = f"{where} hop {position}"
                for col in hop.left:
                    info = current.get(ascii_fold(col))
                    if info is None:
                        raise ConfigError(
                            f"{hop_where}: unknown left key column {col!r} of"
                            f" table {current_table!r}"
                        )
                    _check_usable(info, f"{hop_where} left key {col!r}")
                target_cols = columns_of(hop.table, hop_where)
                for col in hop.right:
                    info = target_cols.get(ascii_fold(col))
                    if info is None:
                        raise ConfigError(
                            f"{hop_where}: unknown right key column {col!r} of"
                            f" table {hop.table!r}"
                        )
                    _check_usable(info, f"{hop_where} right key {col!r}")
                current = target_cols
                current_table = hop.table
            # Entity columns on the terminal table.
            for role, col in (("scheme", rel.entity_scheme),
                              ("identifier", rel.entity_identifier)):
                info = current.get(ascii_fold(col))
                if info is None:
                    raise ConfigError(
                        f"{where}: unknown entity {role} column {col!r} of"
                        f" table {current_table!r}"
                    )
                _check_usable(info, f"{where} entity {role} column")
                if affinity_for_declared_type(info.declared_type) != AFF_TEXT:
                    raise ConfigError(
                        f"{where}: entity {role} column {col!r} of table"
                        f" {current_table!r} must have TEXT affinity (declared"
                        f" {info.declared_type!r}); entity identities are text"
                    )
            resolved.append(ResolvedRelation(config=rel, fields=fields))
        return resolved

    # -- data contract at the read point ------------------------------------

    def check_target_keys(self, conn: sqlite3.Connection) -> None:
        """Every hop's target key must be non-NULL and unique *right now*.

        A violation aborts the whole query; the alternative (duplicating
        detail rows or picking one of several owners) is never acceptable.
        """
        for rel in self.relations:
            for position, hop in enumerate(rel.hops, start=1):
                where = f"relation {rel.name!r} hop {position}"
                cols = ", ".join(quote_ident(c) for c in hop.right)
                null_probe = conn.execute(
                    "SELECT 1 FROM " + quote_ident(hop.table) + " WHERE "
                    + " OR ".join(f"{quote_ident(c)} IS NULL" for c in hop.right)
                    + " LIMIT 1"
                ).fetchone()
                if null_probe is not None:
                    raise ConfigError(
                        f"{where}: target key ({', '.join(hop.right)}) of table"
                        f" {hop.table!r} contains NULL; target keys must be"
                        " non-null at the read point"
                    )
                duplicate = conn.execute(
                    "SELECT " + cols + " FROM " + quote_ident(hop.table)
                    + " GROUP BY " + cols + " HAVING COUNT(*) > 1 LIMIT 1"
                ).fetchone()
                if duplicate is not None:
                    raise ConfigError(
                        f"{where}: target key ({', '.join(hop.right)}) of table"
                        f" {hop.table!r} is not unique at the read point;"
                        " refusing to guess between candidate owners"
                    )


# ---------------------------------------------------------------------------
# JSON loading and structural validation
# ---------------------------------------------------------------------------


def load_catalog(path: str) -> Catalog:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise InvocationError(f"cannot read catalog file {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise InvocationError(f"catalog file {path} is not valid JSON: {exc}") from None
    except UnicodeDecodeError as exc:
        raise InvocationError(f"catalog file {path} is not valid UTF-8: {exc}") from None
    return parse_catalog(document)


def parse_catalog(document) -> Catalog:
    where = "catalog"
    if not isinstance(document, dict):
        raise ConfigError(f"{where}: top level must be a JSON object")
    version = document.get("version")
    if version != CATALOG_VERSION:
        raise ConfigError(
            f"{where}: unsupported version {version!r}; expected {CATALOG_VERSION}"
        )
    raw_relations = document.get("relations")
    if not isinstance(raw_relations, list) or not raw_relations:
        raise ConfigError(f"{where}: 'relations' must be a non-empty array")
    relations = []
    seen_names = set()
    for index, raw in enumerate(raw_relations, start=1):
        rel = _parse_relation(raw, f"relation #{index}")
        folded = ascii_fold(rel.name)
        if folded in seen_names:
            raise ConfigError(
                f"relation {rel.name!r}: duplicate public name (names are"
                " compared case-insensitively)"
            )
        seen_names.add(folded)
        if ascii_fold(rel.name).startswith("sqlite_"):
            raise ConfigError(
                f"relation {rel.name!r}: public names must not start with"
                " 'sqlite_'; that prefix is reserved by SQLite"
            )
        relations.append(rel)
    return Catalog(relations)


def _parse_relation(raw, where: str) -> RelationConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be an object")
    name = _required_text(raw, "name", where)
    source = _required_text(raw, "source", where)
    where = f"relation {name!r}"
    if ascii_fold(name) == ascii_fold(source):
        raise ConfigError(
            f"{where}: public name must differ from its source table"
            f" {source!r} so the access boundary stays explicit"
        )
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ConfigError(f"{where}: 'fields' must be a non-empty array")
    fields = []
    seen_fields = set()
    for item in raw_fields:
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: each field must be an object")
        fname = _required_text(item, "name", where)
        column = item.get("column", fname)
        if not isinstance(column, str) or not column:
            raise ConfigError(
                f"{where} field {fname!r}: 'column' must be a non-empty string"
            )
        folded = ascii_fold(fname)
        if folded in seen_fields:
            raise ConfigError(f"{where}: duplicate field name {fname!r}")
        seen_fields.add(folded)
        fields.append(FieldConfig(name=fname, column=column))

    owner = raw.get("owner")
    if not isinstance(owner, dict):
        raise ConfigError(f"{where}: 'owner' must be an object")
    raw_path = owner.get("path", [])
    if not isinstance(raw_path, list):
        raise ConfigError(f"{where}: owner.path must be an array")
    hops = []
    chain = [ascii_fold(source)]
    for position, item in enumerate(raw_path, start=1):
        hop_where = f"{where} hop {position}"
        if not isinstance(item, dict):
            raise ConfigError(f"{hop_where}: must be an object")
        table = _required_text(item, "table", hop_where)
        left = _key_list(item, "left", hop_where)
        right = _key_list(item, "right", hop_where)
        if len(left) != len(right):
            raise ConfigError(
                f"{hop_where}: left key count {len(left)} does not match"
                f" right key count {len(right)}"
            )
        folded_table = ascii_fold(table)
        if folded_table in chain:
            raise ConfigError(
                f"{hop_where}: owner path revisits table {table!r}; cycles"
                " are not allowed"
            )
        chain.append(folded_table)
        hops.append(HopConfig(table=table, left=left, right=right))
    entity = owner.get("entity")
    if not isinstance(entity, dict):
        raise ConfigError(f"{where}: owner.entity must be an object")
    scheme = _required_text(entity, "scheme", f"{where} owner.entity")
    identifier = _required_text(entity, "identifier", f"{where} owner.entity")

    raw_hidden = raw.get("summary_hidden", [])
    if not isinstance(raw_hidden, list):
        raise ConfigError(f"{where}: 'summary_hidden' must be an array")
    hidden = set()
    for item in raw_hidden:
        if not isinstance(item, str) or not item:
            raise ConfigError(
                f"{where}: summary_hidden entries must be non-empty strings"
            )
        folded = ascii_fold(item)
        if folded not in seen_fields:
            raise ConfigError(
                f"{where}: summary_hidden names {item!r} which is not a"
                " published field"
            )
        hidden.add(folded)
    return RelationConfig(
        name=name,
        source=source,
        fields=tuple(fields),
        hops=tuple(hops),
        entity_scheme=scheme,
        entity_identifier=identifier,
        summary_hidden=frozenset(hidden),
    )


def _required_text(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {key!r} must be a non-empty string")
    return value


def _key_list(obj: dict, key: str, where: str) -> tuple:
    value = obj.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: {key!r} must be a non-empty array of columns")
    out = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ConfigError(
                f"{where}: {key!r} entries must be non-empty strings"
            )
        out.append(item)
    return tuple(out)


# ---------------------------------------------------------------------------
# Physical schema inspection
# ---------------------------------------------------------------------------


def _read_table_columns(conn: sqlite3.Connection, table: str, where: str) -> dict:
    row = conn.execute(
        "SELECT name, type, sql FROM sqlite_master"
        " WHERE name = ? COLLATE NOCASE AND type IN ('table','view')"
        " ORDER BY (name = ? COLLATE BINARY) DESC LIMIT 1",
        (table, table),
    ).fetchone()
    if row is None:
        raise ConfigError(f"{where}: table {table!r} does not exist in main")
    stored_name, object_type, ddl = row
    if object_type != "table":
        raise ConfigError(
            f"{where}: {table!r} is a {object_type}; only ordinary tables in"
            " main can back a public relation"
        )
    ddl = ddl or ""
    if ddl.upper().startswith("CREATE VIRTUAL TABLE"):
        raise ConfigError(f"{where}: {table!r} is a virtual table; not supported")
    declarations = parse_column_definitions(ddl) if ddl else {}
    columns = {}
    for entry in conn.execute(f"PRAGMA table_xinfo({quote_ident(stored_name)})"):
        _cid, colname, decl, _notnull, _dflt, _pkpos = entry[:6]
        hidden = entry[6] if len(entry) > 6 else 0
        if hidden == 1:
            continue  # hidden columns of virtual tables are never readable
        generated = hidden in (2, 3)
        _stored_decl, coll = declarations.get(colname, (None, None))
        columns[ascii_fold(colname)] = ColumnInfo(
            name=colname,
            declared_type=decl or "",
            generated=generated,
            collation=(coll or "BINARY").upper(),
        )
    if not columns:
        raise ConfigError(f"{where}: table {table!r} has no readable columns")
    return columns


def _check_usable(info: ColumnInfo, where: str) -> None:
    if info.generated:
        raise ConfigError(
            f"{where}: column {info.name!r} is a generated column; generated"
            " columns are outside the supported scope"
        )
    if info.collation != "BINARY":
        raise ConfigError(
            f"{where}: column {info.name!r} uses collation {info.collation};"
            " only BINARY-collated columns are supported"
        )
    if info.declared_type and not _DECLARED_TYPE_RE.match(info.declared_type):
        raise ConfigError(
            f"{where}: column {info.name!r} has declared type"
            f" {info.declared_type!r}, outside the supported shape"
            " (plain type names with optional (n) / (n,m))"
        )
