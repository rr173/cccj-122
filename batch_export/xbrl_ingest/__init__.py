"""Local XBRL 2.1 instance ingestion into a queryable SQLite fact warehouse.

Read README.md in this package for the supported subset and the business
identities (context, unit, fact coordinate) that ingestion preserves.
"""
from .dictionary import DictionaryError, load_dictionary
from .errors import (
    ConceptSpec,
    DuplicateIdError,
    IngestError,
    InvalidValueError,
    MissingReferenceError,
    SourceLocation,
    UnknownConceptError,
    XbrlStructureError,
    XbrlXmlError,
)
from .models import (
    ContextCoordinate,
    DeclaredContext,
    DeclaredUnit,
    Entity,
    FactCoordinate,
    FileIngestRecord,
    IngestReport,
    Period,
    QueryResult,
    SourceRecord,
    StoredFact,
    StoredSource,
    UnitCoordinate,
    UnitMeasureTerm,
)
from .service import XbrlIngestor
from .store import XbrlStore

__all__ = [
    "ConceptSpec",
    "ContextCoordinate",
    "DeclaredContext",
    "DeclaredUnit",
    "DictionaryError",
    "DuplicateIdError",
    "Entity",
    "FactCoordinate",
    "FileIngestRecord",
    "IngestError",
    "IngestReport",
    "InvalidValueError",
    "MissingReferenceError",
    "Period",
    "QueryResult",
    "SourceLocation",
    "SourceRecord",
    "StoredFact",
    "StoredSource",
    "UnitCoordinate",
    "UnitMeasureTerm",
    "UnknownConceptError",
    "XbrlIngestor",
    "XbrlStore",
    "XbrlStructureError",
    "XbrlXmlError",
    "load_dictionary",
]
