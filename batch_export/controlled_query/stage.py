"""Staging: resolve row owners and materialize the authorized relations.

Everything here runs inside the source database's read snapshot (one
``BEGIN`` ... ``COMMIT`` on a read-only connection), so the owner chains,
the policy outcome and every business row belong to a single read point
even while the source is in WAL mode and keeps accepting writes.

For each public relation the owner path is evaluated with a chain of
``LEFT JOIN``\ s.  Because :meth:`Catalog.check_target_keys` has already
proved every hop's target key non-null and unique at this read point, each
left row matches **at most one** target row per hop: no fan-out, no
arbitrary choice between candidate owners.  A NULL left key or a missing
target yields a NULL owner, and such rows are invisible to every identity.

Rows whose owner entity is granted ``full`` keep their values; ``summary``
rows have the relation's hidden fields replaced with SQL NULL **here** --
before any user filtering, joining, grouping, aggregation or ordering sees
them.  Rows with no confirming owner or no surviving grant never reach the
private database at all.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .catalog import (
    RelationConfig,
    ResolvedRelation,
    ascii_fold,
    quote_ident,
)
from .errors import ConfigError
from .policy import EFFECT_FULL, EFFECT_SUMMARY


@dataclass
class StagedRelation:
    """The authorized content of one public relation for one identity."""

    name: str
    columns: list  # [(public field name, declared type)] in published order
    rows: list     # [tuple of SQLite values], hidden fields already NULLed
    source_rows: int
    visible_rows: int
    full_rows: int
    summary_rows: int


def stage_all(conn: sqlite3.Connection, resolved: list, effects: dict) -> list:
    """Materialize every public relation for the identity whose visible
    entities are ``effects`` ((scheme, identifier) -> 'full' | 'summary')."""
    return [
        _stage_relation(conn, item, effects)
        for item in resolved
    ]


def _stage_relation(conn: sqlite3.Connection,
                    resolved: ResolvedRelation,
                    effects: dict) -> StagedRelation:
    rel: RelationConfig = resolved.config
    sql = _staging_sql(rel)
    cursor = conn.execute(sql)
    columns = [(field.name, info.declared_type) for field, info in resolved.fields]
    hidden_positions = [
        index
        for index, (field, _info) in enumerate(resolved.fields)
        if ascii_fold(field.name) in rel.summary_hidden
    ] if rel.summary_hidden else []
    rows: list = []
    source_rows = visible = full_rows = summary_rows = 0
    for record in cursor.fetchall():
        source_rows += 1
        owner = _owner_of(record[-2], record[-1], rel)
        if owner is None:
            continue  # no confirmable owner: invisible to everyone
        effect = effects.get(owner)
        if effect is None:
            continue  # no surviving grant for this entity
        visible += 1
        values = list(record[:-2])
        if effect == EFFECT_SUMMARY:
            summary_rows += 1
            for position in hidden_positions:
                values[position] = None
        else:
            full_rows += 1
        rows.append(tuple(values))
    return StagedRelation(
        name=rel.name,
        columns=columns,
        rows=rows,
        source_rows=source_rows,
        visible_rows=visible,
        full_rows=full_rows,
        summary_rows=summary_rows,
    )


def _owner_of(scheme, identifier, rel: RelationConfig) -> Optional[tuple]:
    """The entity tuple for one staged row, or None when no owner can be
    confirmed.  The pair stays a pair; it is never flattened into a single
    string, so distinct schemes with equal identifiers cannot collide."""
    if scheme is None or identifier is None:
        return None
    if not isinstance(scheme, str) or not isinstance(identifier, str):
        raise ConfigError(
            f"relation {rel.name!r}: entity columns must hold TEXT values;"
            f" found {type(scheme).__name__}/{type(identifier).__name__}"
            " at the read point"
        )
    return (scheme, identifier)


def _staging_sql(rel: RelationConfig) -> str:
    field_list = ", ".join(f"t0.{quote_ident(f.column)}" for f in rel.fields)
    terminal = len(rel.hops)
    select = (
        f"SELECT {field_list},"
        f" t{terminal}.{quote_ident(rel.entity_scheme)},"
        f" t{terminal}.{quote_ident(rel.entity_identifier)}"
    )
    parts = [f"{select} FROM {quote_ident(rel.source)} t0"]
    for position, hop in enumerate(rel.hops, start=1):
        condition = " AND ".join(
            f"t{position - 1}.{quote_ident(left)}"
            f" = t{position}.{quote_ident(right)}"
            for left, right in zip(hop.left, hop.right)
        )
        parts.append(
            f"LEFT JOIN {quote_ident(hop.table)} t{position} ON {condition}"
        )
    return " ".join(parts)
