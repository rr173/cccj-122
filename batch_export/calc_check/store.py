"""Read-only access to an existing XBRL SQLite fact warehouse.

The connection is opened with SQLite's ``mode=ro`` URI and no DDL is ever
executed, so a check cannot create, alter or delete any table, and cannot leave
a journal in the warehouse directory. Only the tables written by
:mod:`xbrl_ingest` are read; the original instance files are not needed.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional, Sequence

from .errors import InvocationError


@dataclass(frozen=True)
class FactOccurrence:
    """One stored fact row, with everything a check needs to stay traceable."""

    source_id: str
    position: int
    concept: str
    nil: bool
    decimals: Optional[str]
    value: Optional[Decimal]
    line: int
    column: int
    context_digest: str
    unit_digest: Optional[str]
    coordinate: dict


class ReadOnlyFactStore:
    def __init__(self, db_path: str):
        path = Path(db_path)
        if not path.is_file():
            raise InvocationError(f"SQLite fact warehouse not found: {db_path}")
        uri = f"file:{path.resolve()}?mode=ro"
        try:
            self._conn = sqlite3.connect(uri, uri=True, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise InvocationError(f"cannot open warehouse read-only {db_path}: {exc}") from None
        self._coordinate_cache: dict[tuple[str, Optional[str]], dict] = {}
        self._validate_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReadOnlyFactStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _validate_schema(self) -> None:
        required = {"sources", "facts", "contexts", "units", "context_dimensions",
                    "unit_measures"}
        try:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        except sqlite3.Error as exc:
            raise InvocationError(f"warehouse is not a readable SQLite database: {exc}") from None
        present = {row["name"] for row in rows}
        missing = required - present
        if missing:
            raise InvocationError(
                "warehouse is missing required table(s): " + ", ".join(sorted(missing))
            )

    def require_sources(self, source_ids: Sequence[str]) -> None:
        """Every explicitly selected source must exist; duplicates are harmless."""
        if not source_ids:
            raise InvocationError("at least one source id must be selected")
        unique = tuple(dict.fromkeys(source_ids))
        placeholders = ",".join("?" for _ in unique)
        rows = self._conn.execute(
            f"SELECT source_id FROM sources WHERE source_id IN ({placeholders})", unique
        ).fetchall()
        known = {row["source_id"] for row in rows}
        unknown = [sid for sid in unique if sid not in known]
        if unknown:
            raise InvocationError(
                "unknown source id(s): " + ", ".join(unknown)
                + " (list available sources with the ingest CLI 'sources' command)"
            )

    def source_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT source_id FROM sources ORDER BY ingest_seq"
        ).fetchall()
        return [row["source_id"] for row in rows]

    def load_facts(self, source_ids: Sequence[str]) -> list[FactOccurrence]:
        """All decimal facts of the selected sources, with full coordinates."""
        unique = tuple(dict.fromkeys(source_ids))
        placeholders = ",".join("?" for _ in unique)
        rows = self._conn.execute(
            "SELECT f.source_id, f.position, f.concept, f.nil, f.decimals,"
            " f.value_canonical, f.line, f.column, f.context_digest, f.unit_digest,"
            " c.canonical_json AS context_json, u.canonical_json AS unit_json"
            " FROM facts f"
            " JOIN contexts c ON c.context_digest = f.context_digest"
            " LEFT JOIN units u ON u.unit_digest = f.unit_digest"
            f" WHERE f.concept_type = 'decimal' AND f.source_id IN ({placeholders})"
            " ORDER BY f.source_id, f.position",
            unique,
        ).fetchall()
        occurrences: list[FactOccurrence] = []
        for row in rows:
            coordinate = self._coordinate(
                row["context_digest"], row["unit_digest"],
                row["context_json"], row["unit_json"],
            )
            value = None if row["nil"] else Decimal(row["value_canonical"])
            occurrences.append(FactOccurrence(
                source_id=row["source_id"],
                position=row["position"],
                concept=row["concept"],
                nil=bool(row["nil"]),
                decimals=row["decimals"],
                value=value,
                line=row["line"],
                column=row["column"],
                context_digest=row["context_digest"],
                unit_digest=row["unit_digest"],
                coordinate=coordinate,
            ))
        return occurrences

    def _coordinate(self, context_digest: str, unit_digest: Optional[str],
                    context_json: str, unit_json: Optional[str]) -> dict:
        key = (context_digest, unit_digest)
        cached = self._coordinate_cache.get(key)
        if cached is not None:
            return cached
        coordinate = {
            "context": json.loads(context_json),
            "unit": json.loads(unit_json) if unit_json else None,
        }
        self._coordinate_cache[key] = coordinate
        return coordinate

    def table_snapshot(self) -> dict[str, int]:
        """Row counts per table, used by tests to prove the warehouse is untouched."""
        result = {}
        for table in ("sources", "contexts", "units", "facts", "file_ingests",
                      "context_declarations", "unit_declarations",
                      "context_dimensions", "unit_measures"):
            result[table] = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM {table}"
            ).fetchone()["c"]
        return result
