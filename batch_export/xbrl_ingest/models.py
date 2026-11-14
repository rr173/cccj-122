"""Domain models and canonical identities for ingested XBRL facts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .errors import SourceLocation


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Business coordinates
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entity:
    scheme: str
    identifier: str


@dataclass(frozen=True)
class Period:
    kind: str                      # "instant" | "duration"
    instant: Optional[str] = None  # YYYY-MM-DD for instant
    start: Optional[str] = None    # duration start
    end: Optional[str] = None      # duration end

    def __post_init__(self) -> None:
        if self.kind == "instant":
            if not self.instant or self.start or self.end:
                raise ValueError("instant period needs exactly an instant date")
        elif self.kind == "duration":
            if self.instant or not self.start or not self.end:
                raise ValueError("duration period needs exactly start and end dates")
            if self.end < self.start:
                raise ValueError("duration end date must not precede its start date")
        else:
            raise ValueError(f"unsupported period kind {self.kind!r}")

    def canonical_text(self) -> str:
        if self.kind == "instant":
            return f"instant:{self.instant}"
        return f"duration:{self.start}/{self.end}"


@dataclass(frozen=True)
class ContextCoordinate:
    """What identifies a context in business terms.

    Entity scheme + identifier, period type and dates, and the set of explicit
    dimensions (expanded QName -> expanded member QName), sorted by dimension.
    Context IDs and prefix spellings never enter this identity.
    """

    entity: Entity
    period: Period
    dimensions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "dimensions", tuple(sorted(self.dimensions, key=lambda d: d[0]))
        )
        names = [name for name, _ in self.dimensions]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate dimension in coordinate: {names}")

    def canonical(self) -> dict:
        return {
            "entity": {"scheme": self.entity.scheme, "identifier": self.entity.identifier},
            "period": {
                "kind": self.period.kind,
                "instant": self.period.instant,
                "start": self.period.start,
                "end": self.period.end,
            },
            "dimensions": [[name, member] for name, member in self.dimensions],
        }

    def canonical_text(self) -> str:
        return canonical_json(self.canonical())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UnitMeasureTerm:
    """One measure with its multiplicity in a product/numerator/denominator."""

    measure: str  # expanded QName
    count: int = 1


@dataclass(frozen=True)
class UnitCoordinate:
    """A unit identity: numerator/denominator products, order-insensitive.

    Measure spelling order and prefix do not matter; repeated measures and
    numerator/denominator direction do. USD/shares != shares/USD.
    """

    numerator: tuple[UnitMeasureTerm, ...]
    denominator: tuple[UnitMeasureTerm, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "numerator", _normalise_terms(self.numerator))
        object.__setattr__(self, "denominator", _normalise_terms(self.denominator))
        if not self.numerator:
            raise ValueError("unit has no numerator measure")

    def canonical(self) -> dict:
        return {
            "numerator": [[t.measure, t.count] for t in self.numerator],
            "denominator": [[t.measure, t.count] for t in self.denominator],
        }

    def canonical_text(self) -> str:
        return canonical_json(self.canonical())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()


def _normalise_terms(terms: Sequence[UnitMeasureTerm]) -> tuple[UnitMeasureTerm, ...]:
    merged: dict[str, int] = {}
    for term in terms:
        if term.count < 1:
            raise ValueError("measure multiplicity must be >= 1")
        merged[term.measure] = merged.get(term.measure, 0) + term.count
    return tuple(
        UnitMeasureTerm(measure=measure, count=count)
        for measure, count in sorted(merged.items())
    )


# ---------------------------------------------------------------------------
# Fact coordinates and source models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FactCoordinate:
    """The complete query coordinate of a fact (source path excluded)."""

    concept: str
    context: ContextCoordinate
    unit: Optional[UnitCoordinate]
    language: Optional[str]

    def canonical(self) -> dict:
        return {
            "concept": self.concept,
            "context": self.context.canonical(),
            "unit": self.unit.canonical() if self.unit is not None else None,
            "language": self.language,
        }

    def canonical_text(self) -> str:
        return canonical_json(self.canonical())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeclaredContext:
    """A context as declared in the file (with its file-local identity)."""

    context_id: str
    coordinate: ContextCoordinate
    location: SourceLocation


@dataclass(frozen=True)
class DeclaredUnit:
    unit_id: str
    coordinate: UnitCoordinate
    location: SourceLocation


@dataclass(frozen=True)
class SourceRecord:
    source_id: str          # content-derived identity of the source bytes
    content_sha256: str     # file byte digest
    byte_size: int
    ingested_at: str
    original_name: str
    first_run_id: str


@dataclass(frozen=True)
class StoredFact:
    """A fact row in the warehouse, traceable to its source occurrence."""

    fact_id: int
    source_id: str
    position: int           # 1-based document order of the fact element
    concept: str
    coordinate: FactCoordinate
    context_id_in_source: str
    unit_id_in_source: Optional[str]
    value_text: Optional[str]
    value_canonical: Optional[str]
    value_type: str         # "decimal" | "text"
    nil: bool
    decimals: Optional[str]
    language: Optional[str]
    location: SourceLocation


@dataclass(frozen=True)
class StoredSource:
    source: SourceRecord
    path: str


@dataclass(frozen=True)
class QueryResult:
    """One query coordinate; possibly a single value, nil, or a conflict."""

    coordinate: FactCoordinate
    status: str                     # "single" | "conflict"
    candidates: tuple[StoredFact, ...]
    meaningfully_equal: bool        # all non-nil candidates agree in value
    sources: tuple[str, ...]


@dataclass
class ParsedDocument:
    """In-memory result of parsing one instance file (nothing written yet)."""

    path: str
    content: bytes
    content_sha256: str
    contexts: list[DeclaredContext] = field(default_factory=list)
    units: list[DeclaredUnit] = field(default_factory=list)
    facts: list = field(default_factory=list)  # list[ParsedFact]
    document_element: Optional[str] = None


@dataclass(frozen=True)
class IngestReport:
    source_id: str
    content_sha256: str
    path: str
    run_id: str
    outcome: str            # "ingested" | "already_ingested" | "replaced"
    fact_count: int
    context_count: int
    unit_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileIngestRecord:
    source_id: str
    path: str
    run_id: str
    role: str               # "canonical" | "same-content-other-path"
    recorded_at: str
