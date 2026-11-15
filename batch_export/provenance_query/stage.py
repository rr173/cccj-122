"""SQLite integer/real affinity helpers and value conversion.

The stage design keeps two views of every visible column:

* the **raw** value (``v<i>``, no declared type) -- exactly what the relation
  produced, with its original storage class;
* an **affinity** view (``g<i>``, generated from ``v<i>`` with the column's
  declared type and collation) -- exactly what a bare column reference would
  see in SQLite.

A *direct* column pass-through therefore reads ``v<i>`` (so a projected
literal keeps its storage class across a subquery, as SQLite does), while any
expression that uses the column reads ``g<i>`` (so comparisons, casts within
the expression and COLLATE precedence follow SQLite's affinity rules).
"""
from __future__ import annotations

from .cols import AFF_INTEGER, AFF_NUMERIC, AFF_REAL, AFF_TEXT, OutColumn


def declared_type_for(column: OutColumn) -> str:
    """The declared type that reproduces ``column.affinity``."""
    if not column.has_affinity:
        return ""  # untyped: used only through the raw ``v`` column
    if column.affinity in (AFF_NUMERIC, AFF_INTEGER, AFF_REAL, AFF_TEXT):
        return column.affinity
    return ""  # BLOB affinity comes from having no declared type
