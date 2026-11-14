"""Read-only XBRL calculation relationship checking against a SQLite fact warehouse.

The package consumes:

* an existing fact warehouse produced by :mod:`xbrl_ingest` (opened read-only;
  nothing is ever written back);
* a local calculation linkbase XML file (``link:calculationLink`` /
  ``link:loc`` / ``link:calculationArc``);
* an explicit locator-fragment -> concept mapping table, so no taxonomy is ever
  fetched over the network;
* an explicit, caller-chosen set of source IDs.

It resolves the calculation networks (priority, prohibition, duplicate edges,
cycles), then checks every total at every business coordinate appearing for the
total or any of its direct items, using an explicit closed-interval precision
rule. See ``calc_check/README.md`` for the accepted XML structure, the mapping
format and the exact semantics.
"""
from .errors import (
    CalcCheckError,
    InvocationError,
    LinkbaseError,
    SourceLocation,
    EXIT_OK,
    EXIT_INVOCATION,
    EXIT_DEFINITION,
    EXIT_INCONSISTENT,
)
from .intervals import DecimalInterval, fact_interval, intersect_intervals
from .linkbase import (
    ArcRecord,
    LocRecord,
    ParsedLinkbase,
    load_locator_mapping,
    parse_linkbase_file,
)
from .network import ArcTrace, EffectiveEdge, ResolvedNetwork, resolve_networks
from .service import CalculationChecker

__all__ = [
    "ArcRecord",
    "ArcTrace",
    "CalcCheckError",
    "CalculationChecker",
    "DecimalInterval",
    "EffectiveEdge",
    "EXIT_DEFINITION",
    "EXIT_INCONSISTENT",
    "EXIT_INVOCATION",
    "EXIT_OK",
    "InvocationError",
    "LinkbaseError",
    "LocRecord",
    "ParsedLinkbase",
    "ResolvedNetwork",
    "SourceLocation",
    "fact_interval",
    "intersect_intervals",
    "load_locator_mapping",
    "parse_linkbase_file",
    "resolve_networks",
]
