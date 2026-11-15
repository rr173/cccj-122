"""Column metadata shared by the compiler and the evaluation nodes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

AFF_BLOB = "BLOB"
AFF_TEXT = "TEXT"
AFF_NUMERIC = "NUMERIC"
AFF_INTEGER = "INTEGER"
AFF_REAL = "REAL"

AFFINITIES = frozenset({AFF_BLOB, AFF_TEXT, AFF_NUMERIC, AFF_INTEGER, AFF_REAL})


def affinity_for_declared_type(declared: Optional[str]) -> str:
    """SQLite's column-affinity rules (datatypes.html section 3.1)."""
    t = (declared or "").upper()
    if "INT" in t:
        return AFF_INTEGER
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return AFF_TEXT
    if "BLOB" in t or t == "":
        return AFF_BLOB
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return AFF_REAL
    return AFF_NUMERIC


@dataclass(frozen=True)
class Collation:
    name: str = "BINARY"

    @staticmethod
    def from_sql(name: Optional[str]) -> "Collation":
        if name is None:
            return Collation("BINARY")
        upper = name.upper()
        if upper not in ("BINARY", "NOCASE", "RTRIM"):
            raise ValueError(f"unsupported collation {name!r}")
        return Collation(upper)

    def is_binary(self) -> bool:
        return self.name == "BINARY"


@dataclass(frozen=True)
class OutColumn:
    """One visible output column of a relation.

    ``has_affinity`` distinguishes a *column with an affinity* (a base-table
    column, or a ``CAST`` result, where SQLite applies affinity rules in
    comparisons) from a general *expression*, which SQLite treats as having
    "no affinity" -- a state that differs from BLOB affinity and must be
    reproduced faithfully (see :mod:`provenance_query.render`).
    """

    name: str
    affinity: str = AFF_BLOB
    collation: Collation = Collation("BINARY")
    base_table: Optional[str] = None
    base_column: Optional[str] = None
    has_affinity: bool = True


@dataclass(frozen=True)
class ProvRow:
    """A typed output row plus its provenance annotation node id."""

    values: Optional[tuple]
    annotation: int
