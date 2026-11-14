"""Errors, vocabulary and small value types for the local XBRL ingester."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XBRLI_NS = "http://www.xbrl.org/2003/instance"
XBRLDI_NS = "http://xbrl.org/2006/xbrldi"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# Element names in the XBRL 2.1 instance namespace that this ingester handles.
# Everything else in that namespace is an unsupported structure (tuples, footnote
# links, ...), and any foreign-namespace element inside the instance is refused.
SUPPORTED_XBRLI_LOCALS = frozenset({
    "xbrl", "context", "entity", "identifier", "period", "instant", "startDate",
    "endDate", "segment", "scenario", "explicitMember", "unit", "measure",
    "divide", "unitNumerator", "unitDenominator",
})

ALLOWED_CONCEPT_TYPES = frozenset({"decimal", "text"})

# XML Schema whitespace collapse for xs:date.
_XSD_DATE_RE = r"(\d{4})-(\d{2})-(\d{2})"


class IngestError(Exception):
    """Base error. Every ingestion failure refuses the whole file."""

    def __init__(self, message: str, location: Optional["SourceLocation"] = None):
        self.message = message
        self.location = location
        super().__init__(self._format(message, location))

    @staticmethod
    def _format(message: str, location: Optional["SourceLocation"]) -> str:
        return f"{location}: {message}" if location is not None else message

    def with_location(self, location: "SourceLocation") -> "IngestError":
        if self.location is not None:
            return self
        return type(self)(self.message, location)


class XbrlStructureError(IngestError):
    """The file is not a supported, well-formed XBRL 2.1 instance."""


class XbrlXmlError(XbrlStructureError):
    """The bytes are not well-formed XML or name a prefix that is not bound."""


class UnknownConceptError(XbrlStructureError):
    """A fact names a concept that is not in the local concept dictionary."""


class MissingReferenceError(XbrlStructureError):
    """A fact references a context/unit ID that the file never declares."""


class DuplicateIdError(XbrlStructureError):
    """A context or unit ID is declared more than once."""


class InvalidValueError(XbrlStructureError):
    """A fact/context/unit value is lexically invalid for its declared type."""


@dataclass(frozen=True)
class SourceLocation:
    """Where in the source file something happened (1-based line and column)."""

    path: str
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


@dataclass(frozen=True)
class ConceptSpec:
    """One entry of the local concept dictionary (namespace URI + local name)."""

    namespace: str
    local_name: str
    type: str  # "decimal" | "text"

    def __post_init__(self) -> None:
        if self.type not in ALLOWED_CONCEPT_TYPES:
            raise ValueError(
                f"concept type must be one of {sorted(ALLOWED_CONCEPT_TYPES)}, got {self.type!r}"
            )
        if not self.namespace or not self.local_name:
            raise ValueError("concept namespace and local name must be non-empty")

    @property
    def qname(self) -> str:
        return f"{{{self.namespace}}}{self.local_name}"


@dataclass(frozen=True)
class ParsedDecimal:
    """A decimal fact value preserved exactly, never through binary float."""

    value: Decimal      # exact numeric value (coefficient + exponent preserved)
    canonical: str      # comparable form: no leading +, exponent folded into digits
    text: str           # source text exactly as written (only XML line ends normalised)


@dataclass(frozen=True)
class ParsedFact:
    """A fact as read from the file, before identity resolution."""

    position: int
    concept: str
    concept_type: str
    context_ref: str
    unit_ref: Optional[str]
    nil: bool
    decimals: Optional[str]
    language: Optional[str]
    value_text: Optional[str]       # None iff xsi:nil
    value: Optional[ParsedDecimal]  # only for non-nil decimal facts
    location: SourceLocation
