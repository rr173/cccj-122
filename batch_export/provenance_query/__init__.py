"""Provenance-carrying relational queries over an existing SQLite fact warehouse.

This package adds a *read-only* query mode on top of an existing SQLite
database.  It executes one constrained SELECT statement, collapses all
duplicate result tuples into one ordered typed value tuple with an exact
multiplicity, and records a provenance expression (an integer-weighted
shared DAG over base-row tokens) saying how many derivations produced each
tuple.

Only the produced result file is needed later: reading the compressed results
with their provenance, and excluding base rows from a stored graph, both work
after the source database has been moved away.

Public surface::

    ProvenanceQueryBuilder      build a result file
    ProvenanceResultReader      read a result file back
    evaluate_exclusion          exclude base rows from a stored graph
"""
from .errors import (
    BuildError,
    EXIT_BUILD,
    EXIT_INVOCATION,
    EXIT_UNSUPPORTED,
    InvocationError,
    ProvenanceQueryError,
    UnsupportedQueryError,
)
from .values import (
    TyValue,
    decode_value,
    encode_value,
    make_blob,
    make_float,
    make_int,
    make_null,
    make_text,
    value_key,
    value_sort_key,
)

from .reader import ExclusionError, ProvenanceResultReader, evaluate_exclusion
from .service import BuildReport, ProvenanceQueryBuilder

__all__ = [
    "BuildError",
    "BuildReport",
    "ExclusionError",
    "ProvenanceQueryBuilder",
    "ProvenanceResultReader",
    "EXIT_BUILD",
    "EXIT_INVOCATION",
    "EXIT_UNSUPPORTED",
    "InvocationError",
    "ProvenanceQueryError",
    "TyValue",
    "UnsupportedQueryError",
    "decode_value",
    "evaluate_exclusion",
    "encode_value",
    "make_blob",
    "make_float",
    "make_int",
    "make_null",
    "make_text",
    "value_key",
    "value_sort_key",
]
