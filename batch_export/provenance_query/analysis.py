"""Static analysis of the supported SQLite SELECT subset.

This module validates a parsed statement against the deliberately narrow
subset and provides scope-based name resolution with SQLite's case folding.

Nothing here evaluates data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlglot import exp

from .cols import OutColumn
from .errors import BuildError, UnsupportedQueryError


def ascii_fold(name: str) -> str:
    """SQLite folds only ASCII letters when comparing identifiers."""
    return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in name)


# ---------------------------------------------------------------------------
# Rejection of constructs outside the supported subset
# ---------------------------------------------------------------------------

_AGGREGATES = (
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.GroupConcat,
    exp.Grouping, exp.GroupingId,
)

_REJECTED = {
    exp.Subquery: "subqueries in expressions are not supported",
    exp.Exists: "EXISTS is not supported",
    exp.Window: "window functions are not supported",
    exp.Filter: "aggregate FILTER is not supported",
    exp.Anonymous: "SQL functions are not supported",
    exp.Lambda: "lambda expressions are not supported",
    exp.Placeholder: "bound parameters are not supported",
    exp.Parameter: "bound parameters are not supported",
    exp.Values: "a bare VALUES list is not supported",
    exp.Order: "ORDER BY is not supported",
    exp.Limit: "LIMIT is not supported",
    exp.Offset: "LIMIT/OFFSET is not supported",
    exp.Distinct: "DISTINCT is not supported",
    exp.Intersect: "INTERSECT is not supported",
    exp.Except: "EXCEPT is not supported",
    exp.With: "a nested WITH clause is not supported here",
}

_ALLOWED_SCALAR = (
    exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean, exp.HexString,
    exp.Star, exp.Var,
    exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE,
    exp.Is, exp.NullSafeEQ, exp.NullSafeNEQ, exp.In, exp.Between, exp.Like,
    exp.Glob, exp.Escape, exp.Paren,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.BitwiseAnd,
    exp.BitwiseOr, exp.BitwiseNot, exp.BitwiseLeftShift, exp.BitwiseRightShift,
    exp.DPipe,
    exp.Case, exp.If, exp.Cast, exp.Coalesce, exp.Nullif, exp.Collate,
    exp.DataType, exp.DataTypeParam, exp.Tuple, exp.Alias,
)


def reject_expression(node: exp.Expression, where: str) -> None:
    """Raise if the scalar expression uses an unsupported construct."""
    for sub in node.walk():
        st = type(sub)
        if st in _REJECTED:
            raise UnsupportedQueryError(f"{where}: {_REJECTED[st]}")
        if isinstance(sub, _AGGREGATES):
            raise UnsupportedQueryError(
                f"{where}: aggregate functions are not supported"
            )
        if st is exp.Var:
            parent = sub.parent
            if isinstance(parent, exp.Collate):
                continue
            raise UnsupportedQueryError(f"{where}: unexpected bare word {sub.name!r}")
        if not isinstance(sub, _ALLOWED_SCALAR):
            raise UnsupportedQueryError(
                f"{where}: unsupported expression construct {st.__name__}"
            )


def _reject_select_features(select: exp.Select, where: str) -> None:
    labels = {
        "distinct": "DISTINCT",
        "group": "GROUP BY",
        "having": "HAVING",
        "order": "ORDER BY",
        "limit": "LIMIT",
        "offset": "LIMIT/OFFSET",
        "windows": "WINDOW clauses",
        "qualify": "QUALIFY",
        "cluster": "CLUSTER BY",
        "sample": "SAMPLE",
    }
    for key, label in labels.items():
        if select.args.get(key):
            raise UnsupportedQueryError(f"{where}: {label} is not supported")
    if select.args.get("kind") is not None:
        raise UnsupportedQueryError(f"{where}: unsupported SELECT modifier")


def _reject_union_features(union: exp.Union, where: str) -> None:
    if union.args.get("distinct") is not False:
        raise UnsupportedQueryError(
            f"{where}: only UNION ALL is supported (UNION discards duplicates)"
        )
    if union.args.get("kind") is not None:
        raise UnsupportedQueryError(f"{where}: EXCEPT / INTERSECT are not supported")
    for key, label in (("order", "ORDER BY"), ("limit", "LIMIT"),
                       ("offset", "OFFSET"), ("by_name", "UNION BY NAME")):
        if union.args.get(key):
            raise UnsupportedQueryError(
                f"{where}: {label} is not supported on a compound SELECT"
            )


def _reject_with(with_clause: exp.With, where: str) -> None:
    if with_clause.args.get("recursive"):
        raise UnsupportedQueryError(f"{where}: recursive CTEs are not supported")
    if with_clause.args.get("search") is not None:
        raise UnsupportedQueryError(f"{where}: WITH SEARCH is not supported")
    if with_clause.args.get("cycle") is not None:
        raise UnsupportedQueryError(f"{where}: WITH CYCLE is not supported")
    seen = set()
    for cte in with_clause.expressions:
        name = cte.alias
        key = ascii_fold(name)
        if key in seen:
            raise BuildError(f"{where}: duplicate CTE name {name!r}")
        seen.add(key)
        if cte.args.get("materialized") is not None:
            raise UnsupportedQueryError(
                f"{where}: AS MATERIALIZED / AS NOT MATERIALIZED is not supported"
            )
        body = cte.this
        if not isinstance(body, (exp.Select, exp.Union)):
            raise UnsupportedQueryError(f"{where}: a CTE body must be a SELECT")
        reject_query(body, f"{where} CTE {name}")


def validate_with_reference_order(with_clause: exp.With, where: str) -> None:
    """Reject backward/forward CTE references and self references.

    SQLite's non-recursive WITH makes each CTE visible only to CTEs *after* it
    in declaration order (and to the main query).  A body that references a
    later CTE or itself is a build error, not a lazy runtime failure.
    """
    declared = []
    lower = {}
    for cte in with_clause.expressions:
        name = cte.alias
        declared.append(name)
        lower[name.lower()] = len(declared)
    for position, cte in enumerate(with_clause.expressions, start=1):
        visible = set(declared[: position - 1])
        for table in cte.this.find_all(exp.Table):
            ref = table.name
            if ref.lower() not in lower:
                continue  # a base table, validated later
            if ref.lower() not in {name.lower() for name in visible}:
                raise BuildError(
                    f"{where}: CTE {cte.alias!r} references CTE {ref!r} which is"
                    " not visible: a non-recursive WITH only sees CTEs declared"
                    " before it, and never itself"
                )


def reject_query(tree: exp.Expression, where: str = "query") -> None:
    """Validate the whole statement tree against the supported subset."""
    if isinstance(tree, exp.Union):
        _reject_union_features(tree, where)
        reject_query(tree.this, where)
        reject_query(tree.args["expression"], where)
        if tree.args.get("with_") is not None:
            _reject_with(tree.args["with_"], where)
            validate_with_reference_order(tree.args["with_"], where)
        return
    if tree.args.get("with_") is not None and not isinstance(tree, exp.Select):
        raise UnsupportedQueryError(f"{where}: unsupported query type")
    if not isinstance(tree, exp.Select):
        raise UnsupportedQueryError(
            f"{where}: only SELECT statements are supported, got {type(tree).__name__}"
        )
    _reject_select_features(tree, where)
    if tree.args.get("with_") is not None:
        _reject_with(tree.args["with_"], where)
        validate_with_reference_order(tree.args["with_"], where)
    for source in _from_sources(tree):
        if isinstance(source, exp.Table):
            if source.args.get("db") is not None or source.args.get("catalog") is not None:
                raise UnsupportedQueryError(
                    f"{where}: schema-qualified table names are not supported"
                )
            if not isinstance(source.this, exp.Identifier):
                raise UnsupportedQueryError(
                    f"{where}: table-valued functions are not supported"
                )
        elif isinstance(source, exp.Subquery):
            body = source.this
            if not isinstance(body, (exp.Select, exp.Union)):
                raise UnsupportedQueryError(f"{where}: unsupported derived table")
            reject_query(body, where)
        else:
            raise UnsupportedQueryError(
                f"{where}: unsupported FROM item {type(source).__name__}"
            )
    for join in tree.args.get("joins") or []:
        if join.args.get("side") in ("LEFT", "RIGHT", "FULL"):
            raise UnsupportedQueryError(f"{where}: outer joins are not supported")
        if join.args.get("method") == "NATURAL":
            raise UnsupportedQueryError(f"{where}: NATURAL joins are not supported")
        if join.args.get("using"):
            raise UnsupportedQueryError(f"{where}: JOIN ... USING is not supported")
        if join.args.get("kind") not in (None, "CROSS", "INNER", "STRAIGHT_JOIN"):
            raise UnsupportedQueryError(
                f"{where}: unsupported join kind {join.args.get('kind')!r}"
            )
    for projection in tree.expressions:
        if isinstance(projection, exp.Star):
            continue
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            continue
        reject_expression(projection, where)
    where_clause = tree.args.get("where")
    if where_clause is not None:
        reject_expression(where_clause.this, f"{where} WHERE")
    for join in tree.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None:
            reject_expression(on, f"{where} JOIN ON")


def _from_sources(select: exp.Select):
    from_clause = select.args.get("from_")
    if from_clause is not None and from_clause.this is not None:
        yield from_clause.this
    for join in select.args.get("joins") or []:
        yield join.this


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@dataclass
class ScopeItem:
    alias: str
    columns: list[OutColumn]
    node: object
    base_table: Optional[str] = None


class Scope:
    """Visible FROM entries for one SELECT level."""

    def __init__(self, items: Sequence[ScopeItem]):
        self.items = list(items)
        seen: dict[str, ScopeItem] = {}
        for item in self.items:
            key = ascii_fold(item.alias)
            if key in seen and item.alias:
                raise BuildError(f"duplicate table alias {item.alias!r} in FROM")
            seen[key] = item

    def resolve(self, name: str, table: Optional[str]):
        """Return ``(item, column_index)`` or ``None``.

        Unqualified names must match exactly one visible column; more than one
        match is an ambiguity error.  Qualified names match one alias at this
        level only (a reference never crosses a subquery boundary).
        """
        fold = ascii_fold(name)
        if table is not None:
            tkey = ascii_fold(table)
            for item in self.items:
                if ascii_fold(item.alias) == tkey:
                    for idx, col in enumerate(item.columns):
                        if ascii_fold(col.name) == fold:
                            return item, idx
            return None
        hits = []
        for item in self.items:
            for idx, col in enumerate(item.columns):
                if ascii_fold(col.name) == fold:
                    hits.append((item, idx))
        if not hits:
            return None
        if len(hits) > 1:
            names = ", ".join(f"{it.alias}.{it.columns[i].name}" for it, i in hits)
            raise BuildError(f"ambiguous column {name!r}; it is exposed by {names}")
        return hits[0]
