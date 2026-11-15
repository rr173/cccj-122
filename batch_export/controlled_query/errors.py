"""Errors for the controlled analytical query package.

Every failure is a ``ControlledQueryError`` subclass so callers can catch one
base type.  The command line maps them to distinct non-zero exit codes; a
failed query never leaves a result file behind.
"""
from __future__ import annotations


class ControlledQueryError(Exception):
    """Base class for every error raised by this package."""


class InvocationError(ControlledQueryError):
    """Bad arguments, missing/unreadable files, unknown identity."""


class ConfigError(ControlledQueryError):
    """The relation catalog, the policy file, or the data contract they
    declare (existing plain tables/columns, non-null unique target keys,
    text entity columns) is violated.  Raised before any user SQL runs."""


class UnsupportedQueryError(ControlledQueryError):
    """The SQL statement is outside the approved read-only subset."""


class QueryExecutionError(ControlledQueryError):
    """SQLite failed to execute the approved statement on the staged
    authorized relations (e.g. unknown column, ambiguity, type error)."""


# Exit codes used by the CLI.
EXIT_INVOCATION = 2
EXIT_CONFIG = 3
EXIT_UNSUPPORTED = 4
EXIT_EXECUTION = 5
