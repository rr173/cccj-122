"""Public ingestion API: parse one file, store facts atomically, query and trace."""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Iterable, Optional

from .dictionary import load_dictionary
from .errors import ConceptSpec
from .models import (
    FactCoordinate,
    IngestReport,
    QueryResult,
    StoredFact,
    StoredSource,
)
from .parser import InstanceParser
from .store import XbrlStore


class XbrlIngestor:
    """One ingester bound to one SQLite warehouse and one concept dictionary."""

    def __init__(self, db_path: str, dictionary):
        self._store = XbrlStore(db_path)
        self.concepts: dict[tuple[str, str], ConceptSpec] = load_dictionary(dictionary)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "XbrlIngestor":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def store(self) -> XbrlStore:
        return self._store

    # -- ingestion ------------------------------------------------------------

    def ingest_file(
        self,
        path: str | os.PathLike,
        *,
        system_exit_hook=None,
    ) -> IngestReport:
        """Parse and atomically store one XBRL instance file.

        The file is read once; its byte digest identifies the source. The whole
        file is parsed and validated in memory before a single row is written,
        and the write itself happens in one transaction. Any structural
        problem raises an IngestError naming file and location, leaving the
        warehouse unchanged.
        """
        path_str = str(path)
        content = Path(path_str).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        run_id = f"run_{uuid.uuid4().hex[:20]}"
        document = InstanceParser(path_str, self.concepts).parse(content)
        document.content_sha256 = digest
        return self._store.ingest(document, run_id=run_id, system_exit_hook=system_exit_hook)

    # -- querying -------------------------------------------------------------

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
        return self._store.query(
            concept=concept,
            concept_local=concept_local,
            concept_namespace=concept_namespace,
            entity_scheme=entity_scheme,
            entity_identifier=entity_identifier,
            period_kind=period_kind,
            instant=instant,
            start=start,
            end=end,
            dimensions=dimensions,
            source_id=source_id,
            source_path=source_path,
        )

    def search_text(self, text: str, *, exact: bool = False) -> list[QueryResult]:
        """Find text facts by value (nil and decimals-only facts never match)."""
        return self._store.search_text(text, exact=exact)

    def query_coordinate(self, coordinate: FactCoordinate) -> Optional[QueryResult]:
        return self._store.result_for_digest(coordinate.digest)

    def trace_fact(self, source_id: str, position: int) -> Optional[StoredFact]:
        """Return the stored fact at a source occurrence (1-based, document order)."""
        return self._store.fact_at(source_id, position)

    def trace_fact_id(self, fact_id: int) -> Optional[StoredFact]:
        return self._store.fact_by_id(fact_id)

    def facts_for_source(self, source_id: str) -> list[StoredFact]:
        return self._store.facts_for_source(source_id)

    def sources(self) -> list[StoredSource]:
        return self._store.sources()

    def describe_source(self, source_id: str) -> dict:
        """A JSON-friendly view of one source and its declarations."""
        record = self._store.find_source_by_id(source_id)
        if record is None:
            raise KeyError(source_id)
        return {
            "source": {
                "source_id": record.source_id,
                "content_sha256": record.content_sha256,
                "byte_size": record.byte_size,
                "original_name": record.original_name,
                "ingested_at": record.ingested_at,
            },
            "paths": self._store.paths_of_source(source_id),
            "contexts": [
                {"id_in_source": cid, "coordinate": coord.canonical(),
                 "digest": coord.digest, "location": str(loc)}
                for cid, coord, loc in self._store.contexts_for_source(source_id)
            ],
            "units": [
                {"id_in_source": uid, "coordinate": coord.canonical(),
                 "digest": coord.digest, "location": str(loc)}
                for uid, coord, loc in self._store.units_for_source(source_id)
            ],
        }

    def source_for_path(self, path: str) -> Optional[StoredSource]:
        record = self._store.latest_source_for_path(str(path))
        if record is None:
            return None
        return StoredSource(source=record, path=str(path))

    def contexts_for_source(self, source_id: str):
        return self._store.contexts_for_source(source_id)

    def units_for_source(self, source_id: str):
        return self._store.units_for_source(source_id)
