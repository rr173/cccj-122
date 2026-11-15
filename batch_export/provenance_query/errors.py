"""Errors for the provenance-carrying query package.

All failures are ``ProvenanceQueryError`` subclasses so callers can catch one
base type.  The command line maps them to distinct non-zero exit codes; every
failure path removes partial output, so a result file is either complete and
re-openable or absent.
"""
from __future__ import annotations


class ProvenanceQueryError(Exception):
    """Base class for every error raised by this package."""


class InvocationError(ProvenanceQueryError):
    """Bad arguments, missing files, unknown exclusion identities."""


class UnsupportedQueryError(ProvenanceQueryError):
    """The SQL statement is outside the deliberately supported subset."""


class BuildError(ProvenanceQueryError):
    """The build failed (bad base table, evaluation problem, output exists)."""


# Exit codes used by the CLI.
EXIT_INVOCATION = 2
EXIT_UNSUPPORTED = 3
EXIT_BUILD = 4
