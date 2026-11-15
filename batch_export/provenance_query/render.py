"""Render SQLite scalar expressions to SQL text over a stage layout.

We could use sqlglot's SQLite generator for most nodes, but a few constructs
lose semantics in the generic generator:

* ``CAST(x AS NUMERIC)`` renders as ``CAST(x AS REAL)``;
* ``a IS NOT b`` renders as ``a IS DISTINCT FROM b``;
* ``/`` renders as ``CAST(a AS REAL) / b`` (sqlglot's "safe" division);
* a leading unary ``+`` (which strips column affinity) disappears.

So this module renders every node explicitly.  It also rewrites column
references to the stage's affinity column (``g<i>``) and records direct
pass-through columns separately so a projection can read the raw column
(``v<i>``) when the expression is just that column.
"""
from __future__ import annotations

import re
from typing import Optional

import sqlglot
from sqlglot import exp

from .errors import BuildError, UnsupportedQueryError


class Bound:
    """A rendered expression plus direct-pass-through information.

    When the expression is a plain reference to one visible column,
    ``raw_sql`` names that column's raw stage column (``v<i>``) so the
    projection can pass the value through unchanged; otherwise ``raw_sql``
    is ``None`` and ``sql`` (which references the affinity column ``g<i>``)
    is evaluated.
    """

    __slots__ = ("sql", "raw_sql")

    def __init__(self, sql: str, raw_sql: Optional[str] = None):
        self.sql = sql
        self.raw_sql = raw_sql


def quote_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


_VALUE_TOKEN_TYPES = frozenset({
    "VAR", "NUMBER", "STRING", "IDENTIFIER", "R_PAREN", "BOOLEAN", "NULL",
    "HEX_STRING", "BIT_STRING",
})


def unary_plus_positions(sql: str) -> frozenset[int]:
    """Character offsets of unary ``+`` tokens in the source.

    sqlglot silently drops unary ``+``, whose only semantic effect in SQLite
    is to strip a column reference of its affinity.  We therefore detect the
    token ourselves and render the affected column as its raw stage value.
    """
    positions = set()
    try:
        tokens = sqlglot.tokenize(sql)
    except Exception:  # pragma: no cover - tokenizer already succeeded once
        return frozenset()
    previous = None
    for token in tokens:
        if token.token_type.name == "PLUS":
            if previous is None or previous.token_type.name not in _VALUE_TOKEN_TYPES:
                positions.add(token.start)
        previous = token
    return frozenset(positions)


class Renderer:
    """Renders scalar AST nodes for a given scope and flat stage layout.

    ``qualifiers`` optionally maps ScopeItem objects to a SQL table alias so
    join predicates are unambiguous when both sides use the same column
    letters.
    """

    def __init__(self, scope, source_sql: str, qualifiers=None, plus_positions=None):
        self.scope = scope
        self.source_sql = source_sql
        self.qualifiers = qualifiers or {}
        self.plus_positions = (
            plus_positions if plus_positions is not None
            else unary_plus_positions(source_sql)
        )

    # -- public -----------------------------------------------------------

    def bind(self, node: exp.Expression, where: str) -> Bound:
        core = node
        while isinstance(core, exp.Paren):
            core = core.this
        if isinstance(core, exp.Column) and not isinstance(core.this, exp.Star):
            item, idx = self._resolve(core, where)
            if self.has_unary_plus(core) or not item.columns[idx].has_affinity:
                # ``+col`` / an expression result has no affinity.
                return Bound("(+" + self.raw_local_ref(item, idx) + ")")
            return Bound(self.affinity_ref(item, idx),
                         raw_sql=self.raw_ref(self.flat_index(item, idx)))
        return Bound(self._render(node, where))

    def has_unary_plus(self, node: exp.Expression) -> bool:
        start = _leading_start(node)
        if start is None:
            return False
        probe = start - 1
        while probe >= 0 and self.source_sql[probe] in " \t\r\n(":
            probe -= 1
        return probe in self.plus_positions

    def bind_general(self, node: exp.Expression, where: str) -> str:
        """Render for filters/joins, where both sides use affinity columns."""
        return self._render(node, where)

    def raw_passthrough(self, items, item, column) -> str:
        """Raw stage column for a direct ``*`` expansion of one column."""
        base = 0
        for candidate in items:
            if candidate is item:
                pos = None
                for i, col in enumerate(item.columns):
                    if col is column:
                        pos = i
                        break
                if pos is None:
                    raise BuildError("internal: star column not found")
                flat = base + pos
                ref = self.raw_ref(flat)
                qualifier = self.qualifiers.get(id(item))
                if qualifier is not None:
                    ref = f'{quote_ident(qualifier)}.{ref}'
                return ref
            base += len(candidate.columns)
        raise BuildError("internal: star item not found")

    # -- helpers ----------------------------------------------------------

    def _resolve(self, node: exp.Column, where: str):
        table = node.args.get("table")
        table_name = table.name if table is not None else None
        found = self.scope.resolve(node.name, table_name)
        if found is None:
            if table_name is not None:
                raise BuildError(
                    f"{where}: unknown column {table_name}.{node.name}; the"
                    " qualifier does not match a visible table alias or the"
                    " alias has no such column"
                )
            raise BuildError(f"{where}: unknown column {node.name!r}")
        return found

    def affinity_ref(self, item, column_index: int) -> str:
        return self.local_column_ref(item, column_index)

    def raw_ref(self, flat_index: int) -> str:
        return f'"v{flat_index}"'

    def local_column_ref(self, item, column_index: int) -> str:
        """Affinity column within the item's own physical stage table.

        For a join predicate the right item lives in its own stage, so its
        columns are renumbered from zero there (``g0``, ``g1``, ...).
        """
        qualifier = self.qualifiers.get(id(item))
        if qualifier == "r":
            local = column_index
        else:
            local = self.flat_index(item, column_index)
        ref = f'"g{local}"'
        if qualifier is not None:
            ref = f'{quote_ident(qualifier)}.{ref}'
        return ref

    def raw_local_ref(self, item, column_index: int) -> str:
        """Raw column within the item's own physical stage table."""
        qualifier = self.qualifiers.get(id(item))
        if qualifier == "r":
            local = column_index
        else:
            local = self.flat_index(item, column_index)
        ref = f'"v{local}"'
        if qualifier is not None:
            ref = f'{quote_ident(qualifier)}.{ref}'
        return ref

    def flat_index(self, item, column_index: int) -> int:
        base = 0
        for candidate in self.scope.items:
            if candidate is item:
                return base + column_index
            base += len(candidate.columns)
        raise BuildError("internal: resolved column is not in this scope")

    # -- rendering --------------------------------------------------------

    def _render(self, node: exp.Expression, where: str) -> str:
        handler = _HANDLERS.get(type(node))
        if handler is not None:
            return handler(self, node, where)
        # dynamic fallbacks for classes with inheritance
        if isinstance(node, exp.Binary):
            return self._binary(node, where)
        if isinstance(node, exp.Unary):
            return self._unary(node, where)
        raise UnsupportedQueryError(
            f"{where}: unsupported expression construct {type(node).__name__}"
        )

    def _binary(self, node: exp.Expression, where: str) -> str:
        op = _BINARY_OPS.get(type(node))
        if op is None:
            raise UnsupportedQueryError(
                f"{where}: unsupported operator {type(node).__name__}"
            )
        right = node.args.get("expression")
        if right is None:
            raise UnsupportedQueryError(f"{where}: malformed operator")
        return f"({self._render(node.this, where)} {op} {self._render(right, where)})"

    def _unary(self, node: exp.Expression, where: str) -> str:
        inner = node.this
        if isinstance(node, exp.Not):
            return f"(NOT ({self._render(inner, where)}))"
        if isinstance(node, exp.Neg):
            return f"(-({self._render(inner, where)}))"
        if isinstance(node, exp.BitwiseNot):
            return f"(~({self._render(inner, where)}))"
        raise UnsupportedQueryError(
            f"{where}: unsupported unary operator {type(node).__name__}"
        )


_BINARY_OPS = {
    exp.EQ: "=", exp.NEQ: "<>", exp.LT: "<", exp.LTE: "<=", exp.GT: ">",
    exp.GTE: ">=", exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/",
    exp.Mod: "%", exp.DPipe: "||", exp.BitwiseAnd: "&", exp.BitwiseOr: "|",
    exp.BitwiseLeftShift: "<<", exp.BitwiseRightShift: ">>",
    exp.And: "AND", exp.Or: "OR",
}


def _h_column(r: Renderer, node, where):
    item, idx = r._resolve(node, where)
    column = item.columns[idx]
    if r.has_unary_plus(node) or not column.has_affinity:
        # No affinity: ``+v`` reproduces an expression's affinity-free state
        # (which differs from BLOB-affinity column behaviour).
        return "(+" + r.raw_local_ref(item, idx) + ")"
    return r.local_column_ref(item, idx)


def _h_identifier(r: Renderer, node, where):
    found = r.scope.resolve(node.name, None)
    if found is None:
        raise BuildError(f"{where}: unknown column {node.name!r}")
    item, idx = found
    column = item.columns[idx]
    if r.has_unary_plus(node) or not column.has_affinity:
        return "(+" + r.raw_local_ref(item, idx) + ")"
    return r.local_column_ref(item, idx)


def _h_literal(r: Renderer, node, where):
    if node.is_string:
        return quote_text(node.this)
    return node.this


def _h_null(r, node, where):
    return "NULL"


def _h_boolean(r, node, where):
    return "TRUE" if node.this else "FALSE"


def _h_hex(r, node, where):
    return "X'" + node.this + "'"


def _h_paren(r, node, where):
    return "(" + r._render(node.this, where) + ")"


def _h_alias(r, node, where):
    return r._render(node.this, where)


def _h_case(r: Renderer, node, where):
    parts = ["CASE"]
    base = node.args.get("this")
    if base is not None:
        parts.append(r._render(base, where))
    for branch in node.args.get("ifs") or []:
        parts.append("WHEN " + r._render(branch.this, where))
        parts.append("THEN " + r._render(branch.args["true"], where))
    default = node.args.get("default")
    if default is not None:
        parts.append("ELSE " + r._render(default, where))
    parts.append("END")
    return "(" + " ".join(parts) + ")"


def _h_if(r, node, where):
    raise UnsupportedQueryError(f"{where}: unexpected IF node")


def _h_coalesce(r: Renderer, node, where):
    args = [node.this] + list(node.expressions)
    return "COALESCE(" + ", ".join(r._render(a, where) for a in args) + ")"


def _h_nullif(r, node, where):
    return "NULLIF(" + r._render(node.this, where) + ", " + r._render(node.expression, where) + ")"


def _h_collate(r: Renderer, node, where):
    target = node.expression
    if isinstance(target, exp.Var):
        name = target.name.upper()
    elif isinstance(target, exp.Column):
        name = target.name.upper()
    else:
        raise UnsupportedQueryError(f"{where}: unsupported COLLATE operand")
    if name not in ("BINARY", "NOCASE", "RTRIM"):
        raise UnsupportedQueryError(f"{where}: unsupported collation {name!r}")
    return r._render(node.this, where) + " COLLATE " + name


def _h_is(r: Renderer, node, where):
    right = node.expression
    return "(" + r._render(node.this, where) + " IS " + r._render(right, where) + ")"


def _h_null_safe_eq(r, node, where):
    return "(" + r._render(node.this, where) + " IS " + r._render(node.expression, where) + ")"


def _h_null_safe_neq(r, node, where):
    return "(" + r._render(node.this, where) + " IS NOT " + r._render(node.expression, where) + ")"


def _h_in(r: Renderer, node, where):
    items = node.args.get("expressions")
    if items is None:
        raise UnsupportedQueryError(f"{where}: IN (SELECT ...) subqueries are not supported")
    rendered = ", ".join(r._render(e, where) for e in items)
    return "(" + r._render(node.this, where) + " IN (" + rendered + "))"


def _h_between(r: Renderer, node, where):
    return ("(" + r._render(node.this, where) + " BETWEEN "
            + r._render(node.args["low"], where) + " AND "
            + r._render(node.args["high"], where) + ")")


def _h_like(r: Renderer, node, where):
    negate = node.args.get("negate")
    left = r._render(node.this, where)
    pattern = r._render(node.expression, where)
    if negate:
        return f"(NOT ({left} LIKE {pattern}))"
    return f"({left} LIKE {pattern})"


def _h_glob(r: Renderer, node, where):
    return "(" + r._render(node.this, where) + " GLOB " + r._render(node.expression, where) + ")"


def _h_escape(r: Renderer, node, where):
    inner = node.this
    if not isinstance(inner, exp.Like):
        raise UnsupportedQueryError(f"{where}: ESCAPE without LIKE")
    left = r._render(inner.this, where)
    pattern = r._render(inner.expression, where)
    esc = r._render(node.expression, where)
    return f"({left} LIKE {pattern} ESCAPE {esc})"


def _h_cast(r: Renderer, node, where):
    type_text = _cast_type_text(node, r.source_sql)
    return "CAST(" + r._render(node.this, where) + " AS " + type_text + ")"


_RE_CAST_INNER = re.compile(
    r"^\s*CAST\s*\(\s*(.*?)\s+AS\s+([A-Za-z_][A-Za-z0-9_ ]*(?:\s*\([^)]*\))?)\s*\)\s*$",
    re.I | re.S,
)


def _cast_type_text(node, source_sql: str) -> str:
    """Reproduce the CAST target type exactly as written in the source.

    sqlglot normalises NUMERIC/DECIMAL to a shared DECIMAL type and its SQLite
    generator maps NUMERIC to REAL; both change SQLite's cast semantics.  The
    CAST node carries no source positions, so we recover the type from the
    original text: find the CAST matching this node's operand text position.
    """
    operand_start = _min_start(node.this)
    if operand_start is not None:
        text = source_sql[operand_start:]
        m = _RE_CAST_INNER.match(text)
        if m:
            return m.group(2).strip()
        # The operand may itself be a nested expression; search a local window.
        window_start = max(0, operand_start - 16)
        window = source_sql[window_start:operand_start + 400]
        for candidate in _iter_cast_spans(window):
            inner, type_text = candidate
            if inner.strip() == source_sql[operand_start:operand_start + len(inner)].strip():
                return type_text.strip()
    data_type = node.args.get("to")
    if data_type is None:
        raise UnsupportedQueryError("CAST without a target type")
    return data_type.sql(dialect="sqlite")


def _min_start(node) -> Optional[int]:
    starts = [
        sub.meta["start"]
        for sub in node.walk()
        if isinstance(sub, exp.Identifier) and "start" in sub.meta
    ]
    if not starts:
        starts = [
            sub.meta["start"]
            for sub in node.walk()
            if "start" in sub.meta
        ]
    return min(starts) if starts else None


def _iter_cast_spans(text: str):
    for match in _RE_CAST_ANY.finditer(text):
        yield match.group(1), match.group(2)


_RE_CAST_ANY = re.compile(
    r"CAST\s*\(\s*(.*?)\s+AS\s+([A-Za-z_][A-Za-z0-9_ ]*(?:\s*\([^)]*\))?)\s*\)",
    re.I | re.S,
)


def _h_tuple(r, node, where):
    return "(" + ", ".join(r._render(e, where) for e in node.expressions) + ")"


def _h_var(r, node, where):
    raise UnsupportedQueryError(f"{where}: unexpected bare word {node.name!r}")


_HANDLERS = {
    exp.Column: _h_column,
    exp.Identifier: _h_identifier,
    exp.Literal: _h_literal,
    exp.Null: _h_null,
    exp.Boolean: _h_boolean,
    exp.HexString: _h_hex,
    exp.Paren: _h_paren,
    exp.Alias: _h_alias,
    exp.Case: _h_case,
    exp.If: _h_if,
    exp.Coalesce: _h_coalesce,
    exp.Nullif: _h_nullif,
    exp.Collate: _h_collate,
    exp.Is: _h_is,
    exp.NullSafeEQ: _h_null_safe_eq,
    exp.NullSafeNEQ: _h_null_safe_neq,
    exp.In: _h_in,
    exp.Between: _h_between,
    exp.Like: _h_like,
    exp.Glob: _h_glob,
    exp.Escape: _h_escape,
    exp.Cast: _h_cast,
    exp.Tuple: _h_tuple,
    exp.Var: _h_var,
}


def _leading_start(node: exp.Expression):
    """First source offset of a node's own tokens (Identifier metadata)."""
    starts = [
        sub.meta["start"] for sub in node.walk()
        if isinstance(sub, exp.Identifier) and "start" in sub.meta
    ]
    if not starts:
        starts = [sub.meta["start"] for sub in node.walk() if "start" in sub.meta]
    return min(starts) if starts else None
