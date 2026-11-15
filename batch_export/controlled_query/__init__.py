"""Controlled analytical queries over an existing SQLite warehouse.

This package adds a *locally controlled* query entry point on top of an
existing SQLite fact warehouse.  An administrator publishes a relation
catalog (public relations, their fields, and the owner path to the
enterprise entity ``(scheme, identifier)`` of each row) and an authorization
policy (identities, groups, grants with effects ``full`` / ``summary`` /
``deny``).  A caller names a registered identity and one read-only SELECT;
the statement is executed on a private SQLite database that contains only
the rows and fields that identity may see, with summary-hidden fields
already NULLed before any user computation.

The trust boundary is the SQL received by this API/command: the physical
tables never exist in the execution environment, so no query shape can
reach them.  Whoever holds the warehouse file itself can of course open it
directly; this entry point exists for handing *query access* to analysts
without handing them the file.

Public surface::

    ControlledQueryService   run queries / validate configuration
    load_catalog             parse a relation catalog file
    load_policy              parse an authorization policy file
"""
from .errors import (
    EXIT_CONFIG,
    EXIT_EXECUTION,
    EXIT_INVOCATION,
    EXIT_UNSUPPORTED,
    ConfigError,
    ControlledQueryError,
    InvocationError,
    QueryExecutionError,
    UnsupportedQueryError,
)
from .catalog import Catalog, RelationConfig, load_catalog, parse_catalog
from .policy import Policy, load_policy, parse_policy
from .service import ConfigReport, ControlledQueryService, QueryReport
from .sqlcheck import check_query

__all__ = [
    "Catalog",
    "ConfigError",
    "ConfigReport",
    "ControlledQueryError",
    "ControlledQueryService",
    "EXIT_CONFIG",
    "EXIT_EXECUTION",
    "EXIT_INVOCATION",
    "EXIT_UNSUPPORTED",
    "InvocationError",
    "Policy",
    "QueryExecutionError",
    "QueryReport",
    "RelationConfig",
    "UnsupportedQueryError",
    "check_query",
    "load_catalog",
    "load_policy",
    "parse_catalog",
    "parse_policy",
]
