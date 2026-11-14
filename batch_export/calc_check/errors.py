"""Errors and process exit codes for the calculation checker.

Three distinct failure classes keep terminal automation able to tell a broken
relationship definition or bad invocation apart from facts that really do not
fit the declared relationships:

* :class:`LinkbaseError`  -- the linkbase XML or the locator mapping is invalid
  (the check never runs);
* :class:`InvocationError` -- the caller asked for an unknown source, an
  unreadable database or a concept the dictionary does not know;
* an :data:`EXIT_INCONSISTENT` run -- definitions and invocation are fine, but
  at least one fully-populated coordinate produced an ``inconsistent`` verdict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Process exit codes. They are deliberately distinct so shell automation can
# separate definition/invocation failures from genuine business incompatibility.
EXIT_OK = 0
EXIT_INVOCATION = 2
EXIT_DEFINITION = 3
EXIT_INCONSISTENT = 4

SUMMATION_ITEM_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"

LINK_NS = "http://www.xbrl.org/2003/linkbase"
XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


class CalcCheckError(Exception):
    """Base class for every checker failure."""

    exit_code = EXIT_INVOCATION

    def __init__(self, message: str, location: Optional["SourceLocation"] = None):
        self.message = message
        self.location = location
        super().__init__(SourceLocation.format(message, location))


class InvocationError(CalcCheckError):
    """The check cannot start: unknown source, missing/unreadable input, etc."""

    exit_code = EXIT_INVOCATION


class LinkbaseError(CalcCheckError):
    """The linkbase or locator mapping violates the accepted structure."""

    exit_code = EXIT_DEFINITION


@dataclass(frozen=True)
class SourceLocation:
    """Where in a linkbase/mapping file something happened (1-based)."""

    path: str
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"

    @staticmethod
    def format(message: str, location: Optional["SourceLocation"]) -> str:
        return f"{location}: {message}" if location is not None else message
