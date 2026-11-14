"""SQLite store for XBRL sources, contexts, units and facts (local warehouse).

Layout notes:

* One *source* is the content of one file: its SHA-256 is the identity. Ingestion
  records it with a byte digest, and facts are keyed by source + document position
  so every occurrence stays traceable.
* Contexts and units are stored canonically (their *business coordinates*, not
  their file-local IDs). File-local context/unit IDs are kept only as readable
  trace data on the fact row.
* Duplicate values are never discarded. A query groups fact rows by coordinate
  digest and decides "single value" vs "conflict" from the whole set, not from
  file order.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional

from .errors import SourceLocation
from .models import (
    ContextCoordinate,
    DeclaredContext,
    DeclaredUnit,
    Entity,
    FactCoordinate,
    FileIngestRecord,
    IngestReport,
    ParsedDocument,
    Period,
    QueryResult,
    SourceRecord,
    StoredFact,
    StoredSource,
    UnitCoordinate,
    UnitMeasureTerm,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL UNIQUE,
    byte_size     INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    first_run_id  TEXT NOT NULL,
    ingest_seq    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS file_ingests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(source_id),
    path        TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    role        TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_ingests_path ON file_ingests(path);
CREATE TABLE IF NOT EXISTS contexts (
    context_digest TEXT PRIMARY KEY,
    canonical_json TEXT NOT NULL,
    entity_scheme  TEXT NOT NULL,
    entity_identifier TEXT NOT NULL,
    period_kind    TEXT NOT NULL,
    period_instant TEXT,
    period_start   TEXT,
    period_end     TEXT
);
CREATE TABLE IF NOT EXISTS context_dimensions (
    context_digest TEXT NOT NULL REFERENCES contexts(context_digest),
    dimension      TEXT NOT NULL,
    member         TEXT NOT NULL,
    PRIMARY KEY (context_digest, dimension)
);
CREATE TABLE IF NOT EXISTS context_declarations (
    source_id      TEXT NOT NULL REFERENCES sources(source_id),
    context_id     TEXT NOT NULL,
    context_digest TEXT NOT NULL REFERENCES contexts(context_digest),
    line           INTEGER NOT NULL,
    column         INTEGER NOT NULL,
    PRIMARY KEY (source_id, context_id)
);
CREATE TABLE IF NOT EXISTS units (
    unit_digest    TEXT PRIMARY KEY,
    canonical_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_measures (
    unit_digest TEXT NOT NULL REFERENCES units(unit_digest),
    direction   TEXT NOT NULL,
    measure     TEXT NOT NULL,
    count       INTEGER NOT NULL,
    PRIMARY KEY (unit_digest, direction, measure)
);
CREATE TABLE IF NOT EXISTS unit_declarations (
    source_id  TEXT NOT NULL REFERENCES sources(source_id),
    unit_id    TEXT NOT NULL,
    unit_digest TEXT NOT NULL REFERENCES units(unit_digest),
    line       INTEGER NOT NULL,
    column     INTEGER NOT NULL,
    PRIMARY KEY (source_id, unit_id)
);
CREATE TABLE IF NOT EXISTS facts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        TEXT NOT NULL REFERENCES sources(source_id),
    position         INTEGER NOT NULL,
    concept          TEXT NOT NULL,
    concept_type     TEXT NOT NULL,
    context_digest   TEXT NOT NULL REFERENCES contexts(context_digest),
    unit_digest      TEXT,
    fact_digest      TEXT NOT NULL,
    context_id_in_source TEXT NOT NULL,
    unit_id_in_source    TEXT,
    nil              INTEGER NOT NULL,
    decimals         TEXT,
    language         TEXT,
    value_text       TEXT,
    value_canonical  TEXT,
    line             INTEGER NOT NULL,
    column           INTEGER NOT NULL,
    UNIQUE (source_id, position)
);
CREATE INDEX IF NOT EXISTS idx_facts_digest ON facts(fact_digest);
CREATE INDEX IF NOT EXISTS idx_facts_source ON facts(source_id, position);
CREATE INDEX IF NOT EXISTS idx_facts_concept ON facts(concept);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class XbrlStore:
    """SQLite-backed fact warehouse. Use one connection per command process."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._path_cache: dict[str, str] = {}
        self._conn.executescript(SCHEMA)
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported database schema version {row['value']}; expected {SCHEMA_VERSION}"
            )

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "XbrlStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        """One SQLite transaction. If the process dies mid-transaction (even via
        os._exit inside the test hook) SQLite rolls the whole thing back; no
        partial file is ever visible to a reopened database."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- sources --------------------------------------------------------------

    def find_source_by_digest(self, content_sha256: str) -> Optional[SourceRecord]:
        row = self._conn.execute(
            "SELECT * FROM sources WHERE content_sha256 = ?", (content_sha256,)
        ).fetchone()
        return self._source_from_row(row) if row else None

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> SourceRecord:
        return SourceRecord(
            source_id=row["source_id"],
            content_sha256=row["content_sha256"],
            byte_size=row["byte_size"],
            ingested_at=row["ingested_at"],
            original_name=row["original_name"],
            first_run_id=row["first_run_id"],
        )

    def insert_source(self, record: SourceRecord) -> None:
        seq_row = self._conn.execute(
            "SELECT COALESCE(MAX(ingest_seq), 0) + 1 AS seq FROM sources"
        ).fetchone()
        self._conn.execute(
            "INSERT INTO sources (source_id, content_sha256, byte_size, original_name,"
            " ingested_at, first_run_id, ingest_seq) VALUES (?,?,?,?,?,?,?)",
            (
                record.source_id,
                record.content_sha256,
                record.byte_size,
                record.original_name,
                record.ingested_at,
                record.first_run_id,
                seq_row["seq"],
            ),
        )

    def insert_file_ingest(self, record: FileIngestRecord) -> None:
        self._path_cache.pop(record.source_id, None)
        self._conn.execute(
            "INSERT INTO file_ingests (source_id, path, run_id, role, recorded_at)"
            " VALUES (?,?,?,?,?)",
            (
                record.source_id,
                record.path,
                record.run_id,
                record.role,
                record.recorded_at,
            ),
        )

    def latest_source_for_path(self, path: str) -> Optional[SourceRecord]:
        row = self._conn.execute(
            "SELECT s.* FROM file_ingests f JOIN sources s ON s.source_id = f.source_id"
            " WHERE f.path = ? ORDER BY f.id DESC LIMIT 1",
            (path,),
        ).fetchone()
        return self._source_from_row(row) if row else None

    def find_source_by_id(self, source_id: str) -> Optional[SourceRecord]:
        row = self._conn.execute(
            "SELECT * FROM sources WHERE source_id = ?", (source_id,)
        ).fetchone()
        return self._source_from_row(row) if row else None

    def paths_of_source(self, source_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT path FROM file_ingests WHERE source_id = ? ORDER BY id",
            (source_id,),
        ).fetchall()
        return [row["path"] for row in rows]

    def sources(self) -> list[StoredSource]:
        rows = self._conn.execute(
            "SELECT source_id, content_sha256, byte_size, ingested_at, original_name,"
            " first_run_id FROM sources ORDER BY ingest_seq"
        ).fetchall()
        result: list[StoredSource] = []
        for row in rows:
            record = self._source_from_row(row)
            path_row = self._conn.execute(
                "SELECT path FROM file_ingests WHERE source_id = ? ORDER BY id ASC LIMIT 1",
                (record.source_id,),
            ).fetchone()
            result.append(StoredSource(source=record, path=path_row["path"] if path_row else ""))
        return result

    # -- contexts and units ---------------------------------------------------

    def insert_context(self, declared: DeclaredContext) -> None:
        coordinate = declared.coordinate
        digest = coordinate.digest
        existing = self._conn.execute(
            "SELECT 1 FROM contexts WHERE context_digest = ?", (digest,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO contexts (context_digest, canonical_json, entity_scheme,"
                " entity_identifier, period_kind, period_instant, period_start, period_end)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    digest,
                    coordinate.canonical_text(),
                    coordinate.entity.scheme,
                    coordinate.entity.identifier,
                    coordinate.period.kind,
                    coordinate.period.instant,
                    coordinate.period.start,
                    coordinate.period.end,
                ),
            )
            self._conn.executemany(
                "INSERT INTO context_dimensions (context_digest, dimension, member)"
                " VALUES (?,?,?)",
                [(digest, name, member) for name, member in coordinate.dimensions],
            )

    def insert_unit(self, declared: DeclaredUnit) -> None:
        coordinate = declared.coordinate
        digest = coordinate.digest
        existing = self._conn.execute(
            "SELECT 1 FROM units WHERE unit_digest = ?", (digest,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO units (unit_digest, canonical_json) VALUES (?,?)",
                (digest, coordinate.canonical_text()),
            )
            self._conn.executemany(
                "INSERT INTO unit_measures (unit_digest, direction, measure, count)"
                " VALUES (?,?,?,?)",
                [(digest, "numerator", t.measure, t.count) for t in coordinate.numerator]
                + [(digest, "denominator", t.measure, t.count) for t in coordinate.denominator],
            )

    # -- one atomic ingestion -------------------------------------------------

    def ingest(
        self,
        document: ParsedDocument,
        *,
        run_id: str,
        system_exit_hook=None,
    ) -> IngestReport:
        """Apply one parsed document atomically.

        `system_exit_hook` is a testing-only injection point called after all
        rows of this file have been written but before COMMIT; it is expected
        to terminate the process (e.g. os._exit) so the caller can prove that
        SQLite rolled the whole file back.
        """
        content_digest = document.content_sha256
        existing = self.find_source_by_digest(content_digest)
        if existing is not None:
            with self.transaction():
                self.insert_file_ingest(FileIngestRecord(
                    source_id=existing.source_id,
                    path=document.path,
                    run_id=run_id,
                    role="repeat",
                    recorded_at=utcnow(),
                ))
            return IngestReport(
                source_id=existing.source_id,
                content_sha256=content_digest,
                path=document.path,
                run_id=run_id,
                outcome="already_ingested",
                fact_count=0,
                context_count=0,
                unit_count=0,
            )

        previous = self.latest_source_for_path(document.path)
        outcome = "new-source-same-path" if previous is not None else "ingested"
        source_id = f"src_{content_digest[:24]}"
        now = utcnow()
        warnings: list[str] = []

        with self.transaction():
            self.insert_source(SourceRecord(
                source_id=source_id,
                content_sha256=content_digest,
                byte_size=len(document.content),
                original_name=document.path.rsplit("/", 1)[-1],
                ingested_at=now,
                first_run_id=run_id,
            ))
            self.insert_file_ingest(FileIngestRecord(
                source_id=source_id,
                path=document.path,
                run_id=run_id,
                role="canonical",
                recorded_at=now,
            ))
            for declared in document.contexts:
                self.insert_context(declared)
                self._insert_context_declaration(source_id, declared)
            for declared in document.units:
                self.insert_unit(declared)
                self._insert_unit_declaration(source_id, declared)

            context_by_id = {d.context_id: d for d in document.contexts}
            unit_by_id = {d.unit_id: d for d in document.units}
            rows = []
            for fact in document.facts:
                declared_context = context_by_id[fact.context_ref]
                declared_unit = unit_by_id[fact.unit_ref] if fact.unit_ref else None
                coordinate = FactCoordinate(
                    concept=fact.concept,
                    context=declared_context.coordinate,
                    unit=declared_unit.coordinate if declared_unit else None,
                    language=fact.language,
                )
                rows.append((
                    source_id,
                    fact.position,
                    fact.concept,
                    fact.concept_type,
                    declared_context.coordinate.digest,
                    declared_unit.coordinate.digest if declared_unit else None,
                    coordinate.digest,
                    fact.context_ref,
                    fact.unit_ref,
                    1 if fact.nil else 0,
                    fact.decimals,
                    fact.language,
                    fact.value_text,
                    fact.value.canonical if fact.value else None,
                    fact.location.line,
                    fact.location.column,
                ))
            self._conn.executemany(
                "INSERT INTO facts (source_id, position, concept, concept_type,"
                " context_digest, unit_digest, fact_digest, context_id_in_source,"
                " unit_id_in_source, nil, decimals, language, value_text,"
                " value_canonical, line, column)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            if previous is not None and previous.source_id != source_id:
                # The old source is deliberately NOT deleted: it stays queryable
                # and remains a separate, still-traceable origin of facts.
                warnings.append(
                    f"path {document.path!r} previously held source "
                    f"{previous.source_id}; both sources remain queryable"
                )
            if system_exit_hook is not None:
                system_exit_hook(source_id)

        return IngestReport(
            source_id=source_id,
            content_sha256=content_digest,
            path=document.path,
            run_id=run_id,
            outcome=outcome,
            fact_count=len(document.facts),
            context_count=len(document.contexts),
            unit_count=len(document.units),
            warnings=tuple(warnings),
        )

    def _insert_context_declaration(self, source_id: str, declared: DeclaredContext) -> None:
        self._conn.execute(
            "INSERT INTO context_declarations (source_id, context_id, context_digest,"
            " line, column) VALUES (?,?,?,?,?)",
            (
                source_id,
                declared.context_id,
                declared.coordinate.digest,
                declared.location.line,
                declared.location.column,
            ),
        )

    def _insert_unit_declaration(self, source_id: str, declared: DeclaredUnit) -> None:
        self._conn.execute(
            "INSERT INTO unit_declarations (source_id, unit_id, unit_digest, line, column)"
            " VALUES (?,?,?,?,?)",
            (
                source_id,
                declared.unit_id,
                declared.coordinate.digest,
                declared.location.line,
                declared.location.column,
            ),
        )

    # -- reading --------------------------------------------------------------

    def fact_row_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS c FROM facts").fetchone()["c"]

    def source_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"]

    def query(
        self,
        *,
        concept: Optional[str] = None,
        concept_local: Optional[str] = None,
        concept_namespace: Optional[str] = None,
        entity_scheme: Optional[str] = None,
        entity_identifier: Optional[str] = None,
        period_kind: Optional[str] = None,
        instant: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        dimensions: Optional[Iterable[tuple[str, str]]] = None,
        source_id: Optional[str] = None,
        source_path: Optional[str] = None,
    ) -> list[QueryResult]:
        """Query by concept / entity / period / dimension combinations."""
        sql = [_FACT_SELECT]
        where: list[str] = []
        params: list = []
        if concept is not None:
            where.append("f.concept = ?")
            params.append(concept)
        if concept_local is not None:
            where.append("substr(f.concept, instr(f.concept, '}') + 1) = ?")
            params.append(concept_local)
        if concept_namespace is not None:
            where.append("substr(f.concept, 2, instr(f.concept, '}') - 2) = ?")
            params.append(concept_namespace)
        if entity_scheme is not None:
            where.append("c.entity_scheme = ?")
            params.append(entity_scheme)
        if entity_identifier is not None:
            where.append("c.entity_identifier = ?")
            params.append(entity_identifier)
        if period_kind is not None:
            where.append("c.period_kind = ?")
            params.append(period_kind)
        if instant is not None:
            where.append("c.period_instant = ?")
            params.append(instant)
        if start is not None:
            where.append("c.period_start = ?")
            params.append(start)
        if end is not None:
            where.append("c.period_end = ?")
            params.append(end)
        if source_id is not None:
            where.append("f.source_id = ?")
            params.append(source_id)
        if source_path is not None:
            where.append(
                "f.source_id IN (SELECT source_id FROM file_ingests WHERE path = ?)"
            )
            params.append(source_path)
        for name, member in tuple(dimensions or ()):
            where.append(
                "f.context_digest IN (SELECT context_digest FROM context_dimensions"
                " WHERE dimension = ? AND member = ?)"
            )
            params.extend([name, member])
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY f.fact_digest, source_seq, f.position")
        rows = self._conn.execute(" ".join(sql), params).fetchall()

        return self._group_results(rows)

    def _expand_digests(self, rows) -> list[QueryResult]:
        """Group by coordinate, always returning every candidate of that
        coordinate (a text search on one candidate must not hide its siblings)."""
        digests = []
        seen = set()
        for row in rows:
            if row["fact_digest"] not in seen:
                seen.add(row["fact_digest"])
                digests.append(row["fact_digest"])
        results = []
        for digest in digests:
            full = self._conn.execute(
                _FACT_SELECT + " WHERE f.fact_digest = ? ORDER BY source_seq, f.position",
                (digest,),
            ).fetchall()
            results.extend(self._group_results(full))
        return results

    def _group_results(self, rows) -> list[QueryResult]:
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["fact_digest"], []).append(row)
        results: list[QueryResult] = []
        for fact_digest, group in grouped.items():
            stored = [self._fact_from_row(row) for row in group]
            agree = len({_value_key(f) for f in stored}) == 1
            results.append(QueryResult(
                coordinate=stored[0].coordinate,
                status="single" if agree else "conflict",
                candidates=tuple(stored),
                meaningfully_equal=agree,
                sources=tuple(sorted({f.source_id for f in stored})),
            ))
        results.sort(key=lambda r: (r.coordinate.concept,
                                    r.coordinate.context.canonical_text(),
                                    r.coordinate.unit.canonical_text() if r.coordinate.unit else "",
                                    r.coordinate.language or ""))
        return results

    def search_text(self, text: str, *, exact: bool = False) -> list[QueryResult]:
        """Find text facts by value (literal substring, or exact when asked)."""
        if exact:
            rows = self._conn.execute(
                _FACT_SELECT + " WHERE f.nil = 0 AND f.concept_type = 'text'"
                " AND f.value_text = ? ORDER BY f.fact_digest, source_seq, f.position",
                (text,),
            ).fetchall()
            return self._expand_digests(rows)
        else:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self._conn.execute(
                _FACT_SELECT + " WHERE f.nil = 0 AND f.concept_type = 'text'"
                " AND f.value_text LIKE ? ESCAPE '\\'"
                " ORDER BY f.fact_digest, source_seq, f.position",
                ("%" + escaped + "%",),
            ).fetchall()
        return self._expand_digests(rows)

    def stored_fact_columns(self, fact_id: int) -> Optional[dict]:
        """Raw persisted value columns of one occurrence (no file access)."""
        row = self._conn.execute(
            "SELECT nil, value_text, value_canonical, decimals, language, concept,"
            " source_id, position FROM facts WHERE id = ?", (fact_id,),
        ).fetchone()
        return dict(row) if row else None

    def result_for_digest(self, fact_digest: str) -> Optional[QueryResult]:
        rows = self._conn.execute(
            _FACT_SELECT + " WHERE f.fact_digest = ? ORDER BY source_seq, f.position",
            (fact_digest,),
        ).fetchall()
        if not rows:
            return None
        return self._group_results(rows)[0]

    def facts_for_source(self, source_id: str) -> list[StoredFact]:
        rows = self._conn.execute(
            _FACT_SELECT + " WHERE f.source_id = ? ORDER BY f.position", (source_id,)
        ).fetchall()
        return [self._fact_from_row(row) for row in rows]

    def fact_by_id(self, fact_id: int) -> Optional[StoredFact]:
        row = self._conn.execute(
            _FACT_SELECT + " WHERE f.id = ?", (fact_id,)
        ).fetchone()
        return self._fact_from_row(row) if row else None

    def fact_at(self, source_id: str, position: int) -> Optional[StoredFact]:
        row = self._conn.execute(
            _FACT_SELECT + " WHERE f.source_id = ? AND f.position = ?",
            (source_id, position),
        ).fetchone()
        return self._fact_from_row(row) if row else None

    def contexts_for_source(self, source_id: str) -> list[tuple[str, ContextCoordinate, SourceLocation]]:
        rows = self._conn.execute(
            "SELECT d.context_id, d.line, d.column, c.canonical_json"
            " FROM context_declarations d JOIN contexts c"
            " ON c.context_digest = d.context_digest WHERE d.source_id = ? ORDER BY d.line",
            (source_id,),
        ).fetchall()
        return [
            (row["context_id"],
             _context_from_canonical(row["canonical_json"]),
             SourceLocation(self.path_of_source(source_id), row["line"], row["column"]))
            for row in rows
        ]

    def units_for_source(self, source_id: str) -> list[tuple[str, UnitCoordinate, SourceLocation]]:
        rows = self._conn.execute(
            "SELECT d.unit_id, d.line, d.column, u.canonical_json"
            " FROM unit_declarations d JOIN units u"
            " ON u.unit_digest = d.unit_digest WHERE d.source_id = ? ORDER BY d.line",
            (source_id,),
        ).fetchall()
        return [
            (row["unit_id"],
             _unit_from_canonical(row["canonical_json"]),
             SourceLocation(self.path_of_source(source_id), row["line"], row["column"]))
            for row in rows
        ]

    def path_of_source(self, source_id: str) -> str:
        cached = self._path_cache.get(source_id)
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT path FROM file_ingests WHERE source_id = ? ORDER BY id ASC LIMIT 1",
            (source_id,),
        ).fetchone()
        path = row["path"] if row else ""
        self._path_cache[source_id] = path
        return path

    def run_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT run_id FROM file_ingests ORDER BY run_id"
        ).fetchall()
        return [r["run_id"] for r in rows]

    # -- row mapping ----------------------------------------------------------

    def _fact_from_row(self, row: sqlite3.Row) -> StoredFact:
        path = row["source_path"]
        return StoredFact(
            fact_id=row["id"],
            source_id=row["source_id"],
            position=row["position"],
            concept=row["concept"],
            coordinate=FactCoordinate(
                concept=row["concept"],
                context=_context_from_canonical(row["context_json"]),
                unit=_unit_from_canonical(row["unit_json"]) if row["unit_json"] else None,
                language=row["language"],
            ),
            context_id_in_source=row["context_id_in_source"],
            unit_id_in_source=row["unit_id_in_source"],
            value_text=row["value_text"],
            value_canonical=row["value_canonical"],
            value_type=row["concept_type"],
            nil=bool(row["nil"]),
            decimals=row["decimals"],
            language=row["language"],
            location=SourceLocation(path, row["line"], row["column"]),
        )


_FACT_SELECT = (
    "SELECT f.*, c.canonical_json AS context_json, u.canonical_json AS unit_json,"
    " (SELECT path FROM file_ingests fi WHERE fi.source_id = f.source_id"
    "  ORDER BY fi.id ASC LIMIT 1) AS source_path,"
    " (SELECT ingest_seq FROM sources s WHERE s.source_id = f.source_id) AS source_seq"
    " FROM facts f"
    " JOIN contexts c ON c.context_digest = f.context_digest"
    " LEFT JOIN units u ON u.unit_digest = f.unit_digest"
)


def _context_from_canonical(text: str) -> ContextCoordinate:
    import json
    data = json.loads(text)
    entity = Entity(scheme=data["entity"]["scheme"], identifier=data["entity"]["identifier"])
    period_data = data["period"]
    period = Period(
        kind=period_data["kind"],
        instant=period_data["instant"],
        start=period_data["start"],
        end=period_data["end"],
    )
    return ContextCoordinate(
        entity=entity, period=period,
        dimensions=tuple((name, member) for name, member in data["dimensions"]),
    )


def _unit_from_canonical(text: str) -> UnitCoordinate:
    import json
    data = json.loads(text)
    return UnitCoordinate(
        numerator=tuple(UnitMeasureTerm(m, c) for m, c in data["numerator"]),
        denominator=tuple(UnitMeasureTerm(m, c) for m, c in data["denominator"]),
    )


def _value_key(fact: StoredFact) -> tuple:
    """What makes two candidates the same value (ignoring source and format)."""
    if fact.nil:
        return ("nil",)
    if fact.value_type == "decimal":
        return ("decimal", fact.value_canonical)
    return ("text", fact.value_text)


