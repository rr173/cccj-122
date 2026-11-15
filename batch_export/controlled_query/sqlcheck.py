"""Structural validation of the approved read-only SQL subset.

The gate parses one statement with SQLGlot (SQLite dialect) and walks the
syntax tree with an explicit CTE name scope.  It is deliberately an
*allowlist*: every query shape, expression construct and function that is
not listed here is rejected, and anything SQLGlot produces that this module
does not explicitly understand is rejected too (unknown truthy node
arguments fail closed).

Approved syntax
---------------

* one single ``SELECT`` (optionally with a non-recursive ``WITH``) or
  ``UNION ALL`` chain; ``ORDER BY`` / ``LIMIT`` / ``OFFSET``;
* non-recursive CTEs (each sees only the CTEs declared before it, never
  itself), derived tables, correlated ``EXISTS`` / ``NOT EXISTS``,
  ``IN (SELECT ...)`` and ``IN (value, ...)``;
* ``INNER`` / ``CROSS`` / ``LEFT [OUTER] JOIN ... ON`` (comma joins count as
  cross joins); no ``RIGHT``/``FULL``/``NATURAL`` joins, no ``USING``;
* ``WHERE``, ``GROUP BY``, ``HAVING``;
* comparisons (``= != < <= > >= IS [NOT] BETWEEN IN LIKE [ESCAPE]``),
  boolean ``AND/OR/NOT``, arithmetic ``+ - * / %``, ``||``, unary minus;
* ``CASE``, ``CAST``, ``COLLATE`` (built-in SQLite collations only exist);
* approved functions only: ``COUNT SUM AVG MIN MAX`` (aggregates) and
  ``COALESCE NULLIF ABS LENGTH SUBSTR TYPEOF`` (scalar);
* numeric/text/blob literals, ``NULL``, ``TRUE``/``FALSE``, quoted
  identifiers (including names with spaces), ``*`` and ``alias.*``.

Everything else is rejected: writes, ``PRAGMA``, ``ATTACH``, multiple
statements, (schema-qualified names like ``main.t``), table-valued
functions, physical/metadata tables (they simply are not public relations),
``DISTINCT``, window functions, ``FILTER``, recursive CTEs, scalar
subqueries, ``VALUES`` lists, bound parameters, ``RIGHT``/``FULL`` joins and
any function not on the approved list (including ``load_extension`` and
``random``).

Note the trust boundary does **not** rest on this gate alone: the approved
statement is executed on a private SQLite database that contains only the
authorized rows of the public relations.  The gate exists so that supported
syntax is explicit and anything unsupported fails loudly instead of being
silently mis-evaluated.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .catalog import Catalog, ascii_fold
from .errors import UnsupportedQueryError


@dataclass
class CheckedQuery:
    sql: str
    relations: list  # canonical public relation names referenced, sorted


def check_query(sql_text: str, catalog: Catalog) -> CheckedQuery:
    tree = parse_sql_text(sql_text)
    relations = {ascii_fold(name): name for name in catalog.public_names()}
    referenced: set = set()
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise UnsupportedQueryError(
            f"only a single SELECT statement is supported, got"
            f" {type(tree).__name__}"
        )
    _check_query(tree, None, relations, referenced, "query")
    return CheckedQuery(sql=sql_text,
                        relations=sorted(referenced, key=ascii_fold))


def parse_sql_text(text: str) -> exp.Expression:
    stripped = text.strip()
    if not stripped:
        raise UnsupportedQueryError("the SQL text is empty")
    try:
        statements = sqlglot.parse(stripped, read="sqlite")
    except (ParseError, TokenError) as exc:
        raise UnsupportedQueryError(f"cannot parse the SQL text: {exc}") from None
    statements = [s for s in statements if s is not None]
    if not statements:
        raise UnsupportedQueryError("the SQL text contains no statement")
    if len(statements) != 1:
        raise UnsupportedQueryError(
            f"exactly one statement is required; found {len(statements)}"
        )
    return statements[0]


# ---------------------------------------------------------------------------
# CTE name scope
# ---------------------------------------------------------------------------


class _CteScope:
    """The CTE names visible at one point, chained outward."""

    __slots__ = ("names", "parent")

    def __init__(self, names: dict, parent):
        self.names = names
        self.parent = parent

    def visible(self, folded: str) -> bool:
        scope = self
        while scope is not None:
            if folded in scope.names:
                return True
            scope = scope.parent
        return False


# ---------------------------------------------------------------------------
# Query-level checks
# ---------------------------------------------------------------------------

_HANDLED_SELECT_KEYS = {
    "expressions", "from_", "joins", "where", "group", "having",
    "order", "limit", "offset", "with_",
}
_REJECTED_SELECT_KEYS = {
    "distinct": "DISTINCT",
    "qualify": "QUALIFY",
    "windows": "WINDOW clauses",
    "cluster": "CLUSTER BY",
    "sample": "SAMPLE",
    "kind": "SELECT modifiers",
    "hint": "optimizer hints",
    "exclude": "EXCLUDE",
    "operation_modifiers": "SELECT modifiers",
    "locks": "locking clauses",
    "into": "SELECT INTO",
}


def _reject_unknown_args(node, handled: set, where: str, what: str) -> None:
    for key, value in node.args.items():
        if key in handled or value is None or value == [] or value is False:
            continue
        raise UnsupportedQueryError(
            f"{where}: unsupported {what} feature (argument {key!r})"
        )


def _check_query(node, scope, relations, referenced, where) -> None:
    if isinstance(node, exp.Subquery):
        # A parenthesized query (e.g. a UNION arm) is just grouping.
        node = node.this
    with_clause = node.args.get("with_")
    if with_clause is not None:
        scope = _apply_with(with_clause, scope, relations, referenced, where)
    if isinstance(node, exp.Select):
        _check_select(node, scope, relations, referenced, where)
        return
    if isinstance(node, exp.Union):
        _check_union(node, scope, relations, referenced, where)
        return
    raise UnsupportedQueryError(
        f"{where}: only SELECT / UNION ALL queries are supported, got"
        f" {type(node).__name__}"
    )


def _apply_with(with_clause, scope, relations, referenced, where):
    if with_clause.args.get("recursive"):
        raise UnsupportedQueryError(f"{where}: recursive CTEs are not supported")
    for key, label in (("search", "WITH SEARCH"), ("cycle", "WITH CYCLE")):
        if with_clause.args.get(key):
            raise UnsupportedQueryError(f"{where}: {label} is not supported")
    level: dict = {}
    inner = _CteScope(level, scope)
    for cte in with_clause.expressions:
        name = cte.alias
        folded = ascii_fold(name)
        if not name:
            raise UnsupportedQueryError(f"{where}: a CTE needs a name")
        if folded in level:
            raise UnsupportedQueryError(
                f"{where}: duplicate CTE name {name!r} at the same level"
            )
        if cte.args.get("materialized") is not None:
            raise UnsupportedQueryError(
                f"{where} CTE {name!r}: MATERIALIZED / NOT MATERIALIZED is"
                " not supported"
            )
        _reject_unknown_args(cte, {"this", "alias", "materialized"},
                             f"{where} CTE {name!r}", "CTE")
        body = cte.this
        if not isinstance(body, (exp.Select, exp.Union)):
            raise UnsupportedQueryError(
                f"{where} CTE {name!r}: the body must be a SELECT"
            )
        # Only the CTEs declared *before* this one are visible in its body;
        # the name itself is added afterwards (non-recursive semantics).
        _check_query(body, inner, relations, referenced, f"{where} CTE {name!r}")
        level[folded] = name
    return inner


def _check_union(node, scope, relations, referenced, where) -> None:
    if node.args.get("distinct") is not False:
        raise UnsupportedQueryError(
            f"{where}: only UNION ALL is supported (UNION / UNION DISTINCT"
            " discard duplicates)"
        )
    _reject_unknown_args(
        node, {"this", "expression", "distinct", "order", "limit", "offset",
               "with_"},
        where, "compound SELECT",
    )
    _check_query(node.this, scope, relations, referenced, where)
    _check_query(node.args["expression"], scope, relations, referenced, where)
    _check_order_limit(node, scope, relations, referenced, where)


def _check_select(select, scope, relations, referenced, where) -> None:
    for key, value in select.args.items():
        if key in _HANDLED_SELECT_KEYS or value is None or value == [] \
                or value is False:
            continue
        label = _REJECTED_SELECT_KEYS.get(key)
        if label is None:
            raise UnsupportedQueryError(
                f"{where}: unsupported SELECT feature (argument {key!r})"
            )
        raise UnsupportedQueryError(f"{where}: {label} is not supported")

    from_clause = select.args.get("from_")
    if from_clause is not None and from_clause.this is not None:
        _check_from_item(from_clause.this, scope, relations, referenced, where)
    for join in select.args.get("joins") or []:
        _check_join(join, scope, relations, referenced, where)
    for projection in select.expressions:
        _check_scalar(projection, scope, relations, referenced, where)
    where_clause = select.args.get("where")
    if where_clause is not None:
        _check_scalar(where_clause.this, scope, relations, referenced,
                      f"{where} WHERE")
    group = select.args.get("group")
    if group is not None:
        _reject_unknown_args(group, {"expressions"}, where, "GROUP BY")
        for item in group.expressions:
            _check_scalar(item, scope, relations, referenced,
                          f"{where} GROUP BY")
    having = select.args.get("having")
    if having is not None:
        _check_scalar(having.this, scope, relations, referenced,
                      f"{where} HAVING")
    _check_order_limit(select, scope, relations, referenced, where)


def _check_order_limit(node, scope, relations, referenced, where) -> None:
    order = node.args.get("order")
    if order is not None:
        _reject_unknown_args(order, {"expressions"}, where, "ORDER BY")
        for item in order.expressions:
            if not isinstance(item, exp.Ordered):
                raise UnsupportedQueryError(
                    f"{where}: unsupported ORDER BY item"
                    f" {type(item).__name__}"
                )
            _reject_unknown_args(
                item, {"this", "desc", "nulls_first", "with_fill"},
                where, "ORDER BY item",
            )
            _check_scalar(item.this, scope, relations, referenced,
                          f"{where} ORDER BY")
    for key, label in (("limit", "LIMIT"), ("offset", "OFFSET")):
        clause = node.args.get(key)
        if clause is None:
            continue
        _reject_unknown_args(clause, {"this", "expression", "expressions"},
                             where, label)
        for value in (clause.args.get("this"), clause.args.get("expression")):
            if isinstance(value, exp.Expression):
                _check_scalar(value, scope, relations, referenced,
                              f"{where} {label}")


def _check_from_item(item, scope, relations, referenced, where) -> None:
    if isinstance(item, exp.Table):
        _check_table(item, scope, relations, referenced, where)
        return
    if isinstance(item, exp.Subquery):
        _reject_unknown_args(item, {"this", "alias", "pivots"}, where,
                             "derived table")
        if item.args.get("pivots"):
            raise UnsupportedQueryError(f"{where}: PIVOT is not supported")
        body = item.this
        if not isinstance(body, (exp.Select, exp.Union)):
            raise UnsupportedQueryError(
                f"{where}: a derived table must wrap a SELECT"
            )
        _check_query(body, scope, relations, referenced,
                     f"{where} derived table")
        return
    raise UnsupportedQueryError(
        f"{where}: unsupported FROM item {type(item).__name__}"
    )


def _check_table(table, scope, relations, referenced, where) -> None:
    _reject_unknown_args(table, {"this", "db", "catalog", "alias"}, where,
                         "table reference")
    if table.args.get("db") is not None or table.args.get("catalog") is not None:
        raise UnsupportedQueryError(
            f"{where}: schema-qualified table names (e.g. main.t) are not"
            " supported; refer to public relations by their plain name"
        )
    if not isinstance(table.this, exp.Identifier):
        raise UnsupportedQueryError(
            f"{where}: table-valued functions are not supported"
        )
    name = table.name
    folded = ascii_fold(name)
    if scope is not None and scope.visible(folded):
        return  # a CTE reference; its body was checked at declaration
    canonical = relations.get(folded)
    if canonical is None:
        raise UnsupportedQueryError(
            f"{where}: {name!r} is not a public relation; only the"
            " administrator-published relations can be queried"
        )
    referenced.add(canonical)


def _check_join(join, scope, relations, referenced, where) -> None:
    if join.args.get("method") == "NATURAL":
        raise UnsupportedQueryError(f"{where}: NATURAL joins are not supported")
    if join.args.get("using"):
        raise UnsupportedQueryError(
            f"{where}: JOIN ... USING is not supported; write the ON"
            " condition explicitly"
        )
    _reject_unknown_args(join, {"this", "side", "kind", "method", "on", "using"},
                         where, "join")
    side = join.args.get("side")
    kind = join.args.get("kind")
    allowed = (
        (side is None and kind in (None, "INNER", "CROSS"))
        or (side == "LEFT" and kind in (None, "OUTER"))
    )
    if not allowed:
        raise UnsupportedQueryError(
            f"{where}: only INNER, CROSS and LEFT joins are supported"
        )
    _check_from_item(join.this, scope, relations, referenced, where)
    on = join.args.get("on")
    if on is not None:
        _check_scalar(on, scope, relations, referenced, f"{where} JOIN ON")


# ---------------------------------------------------------------------------
# Scalar expressions
# ---------------------------------------------------------------------------

_APPROVED_FUNCS = {
    exp.Count: "COUNT", exp.Sum: "SUM", exp.Avg: "AVG",
    exp.Min: "MIN", exp.Max: "MAX",
    exp.Coalesce: "COALESCE", exp.Nullif: "NULLIF", exp.Abs: "ABS",
    exp.Length: "LENGTH", exp.Substring: "SUBSTR", exp.Typeof: "TYPEOF",
}

_ALLOWED_NODES = (
    exp.Column, exp.Identifier, exp.Star, exp.Literal, exp.Null, exp.Boolean,
    exp.HexString, exp.Paren,
    exp.And, exp.Or, exp.Not,
    exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Is,
    exp.Between, exp.Like, exp.Escape,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.DPipe,
    exp.Case, exp.If, exp.Cast, exp.DataType, exp.DataTypeParam,
    exp.Alias, exp.Ordered,
) + tuple(_APPROVED_FUNCS)


def _check_scalar(node, scope, relations, referenced, where) -> None:
    if isinstance(node, exp.Subquery):
        raise UnsupportedQueryError(
            f"{where}: scalar subqueries are not supported (EXISTS and"
            " IN (SELECT ...) are)"
        )
    if isinstance(node, exp.Exists):
        _reject_unknown_args(node, {"this"}, where, "EXISTS")
        body = node.this
        if isinstance(body, exp.Subquery):
            body = body.this
        if not isinstance(body, (exp.Select, exp.Union)):
            raise UnsupportedQueryError(f"{where}: EXISTS must wrap a SELECT")
        _check_query(body, scope, relations, referenced, f"{where} EXISTS")
        return
    if isinstance(node, exp.In):
        _reject_unknown_args(node, {"this", "query", "expressions"}, where,
                             "IN")
        _check_scalar(node.this, scope, relations, referenced, where)
        query = node.args.get("query")
        if query is not None:
            body = query.this if isinstance(query, exp.Subquery) else query
            if not isinstance(body, (exp.Select, exp.Union)):
                raise UnsupportedQueryError(
                    f"{where}: IN must wrap a SELECT or a value list"
                )
            _check_query(body, scope, relations, referenced, f"{where} IN")
        for item in node.args.get("expressions") or []:
            _check_scalar(item, scope, relations, referenced, where)
        return
    if isinstance(node, exp.Collate):
        _reject_unknown_args(node, {"this", "expression"}, where, "COLLATE")
        _check_scalar(node.this, scope, relations, referenced, where)
        return
    if isinstance(node, exp.Column):
        if node.args.get("db") is not None or node.args.get("catalog") is not None:
            raise UnsupportedQueryError(
                f"{where}: schema-qualified column names are not supported"
            )
    if not isinstance(node, _ALLOWED_NODES):
        if isinstance(node, exp.Func):
            if isinstance(node, exp.Anonymous):
                name = str(node.this)
            elif hasattr(node, "sql_name"):
                name = node.sql_name()
            else:  # pragma: no cover - defensive
                name = type(node).__name__
            raise UnsupportedQueryError(
                f"{where}: function {str(name).upper()!r} is not approved;"
                " approved functions: COUNT, SUM, AVG, MIN, MAX, COALESCE,"
                " NULLIF, ABS, LENGTH, SUBSTR, TYPEOF"
            )
        raise UnsupportedQueryError(
            f"{where}: unsupported expression construct"
            f" {type(node).__name__}"
        )
    for value in node.args.values():
        _check_arg_value(value, scope, relations, referenced, where)


def _check_arg_value(value, scope, relations, referenced, where) -> None:
    if isinstance(value, exp.Expression):
        _check_scalar(value, scope, relations, referenced, where)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, exp.Expression):
                _check_scalar(item, scope, relations, referenced, where)
