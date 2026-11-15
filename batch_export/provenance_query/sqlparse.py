"""Parse exactly one SQLite SQL statement from a file's text."""
from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .errors import UnsupportedQueryError


def parse_sql_text(text: str, *, origin: str = "SQL") -> exp.Expression:
    """Return the single statement parsed with SQLite name/expression rules."""
    stripped = text.strip()
    if not stripped:
        raise UnsupportedQueryError(f"{origin} is empty")
    try:
        statements = sqlglot.parse(stripped, read="sqlite")
    except (ParseError, TokenError) as exc:
        raise UnsupportedQueryError(f"cannot parse {origin}: {exc}") from None
    statements = [s for s in statements if s is not None]
    if not statements:
        raise UnsupportedQueryError(f"{origin} contains no statement")
    if len(statements) != 1:
        raise UnsupportedQueryError(
            f"{origin} must contain exactly one statement; found {len(statements)}"
        )
    return statements[0]


def parse_sql_file(path: str) -> tuple[exp.Expression, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise UnsupportedQueryError(f"cannot read SQL file {path}: {exc}") from None
    except UnicodeDecodeError as exc:
        raise UnsupportedQueryError(f"SQL file {path} is not valid UTF-8: {exc}") from None
    return parse_sql_text(text, origin=path), text
