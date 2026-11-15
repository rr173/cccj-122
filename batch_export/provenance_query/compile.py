"""Turn a validated SQLGlot AST into a provenance-bearing plan.

``compile_query`` is the single entry point.  It walks the AST, resolves names
against the exact SQLite scoping rules, and builds the node tree.

Resolution strategy
-------------------
Name resolution is done statically *per SELECT level*:

* each FROM item exposes its columns with the item's alias;
* unqualified references must match exactly one visible item, otherwise it's
  an ambiguity error (never silently resolved left-to-right);
* qualified references (``t.x``) match one item alias at this level only.

A CTE is compiled once per defining level (so a CTE referenced twice shares
one node, one tokenisation pass, and one annotation tree); the two references
get *distinct alias names* but the same node instance.  Because a node's rows
are produced once and cached, the same base row keeps the same token through
both references -- exactly the required semantics for UNION ALL of the same
branch and for self-joins.

Data evaluation happens later, node by node, in a private connection.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import sqlglot
from sqlglot import exp

from .analysis import (
    Scope,
    ScopeItem,
    ascii_fold,
    reject_query,
)
from .catalog import Catalog, quote
from .cols import AFF_BLOB, Collation, OutColumn
from .errors import BuildError, UnsupportedQueryError
from .graph import ProvenanceGraph
from .render import Bound, Renderer
from .runtime import (
    BaseScan,
    Const,
    CrossJoin,
    Filter,
    Join,
    PlanNode,
    Project,
    UnionAll,
)


@dataclass
class QueryPlan:
    root: PlanNode
    columns: list[OutColumn]
    graph: ProvenanceGraph


class Compiler:
    def __init__(self, conn, source_sql: str):
        self.conn = conn
        self.source_sql = source_sql
        self.catalog = Catalog(conn)
        self.graph = ProvenanceGraph()

    def compile(self, tree: exp.Expression) -> QueryPlan:
        reject_query(tree, "query")
        node, columns = self._compile_query(tree, ctes={})
        return QueryPlan(node, columns, self.graph)

    # -- query dispatch ---------------------------------------------------

    def _compile_query(self, tree, ctes):
        if isinstance(tree, exp.Union):
            return self._compile_union(tree, ctes)
        return self._compile_select(tree, ctes)

    # -- CTEs -------------------------------------------------------------

    def _compile_with(self, with_clause, ctes: dict) -> dict:
        out = dict(ctes)
        for cte in with_clause.expressions:
            name = cte.alias
            if not name:
                raise BuildError("CTE without a name")
            if ascii_fold(name) in out:
                raise BuildError(f"duplicate CTE name {name!r}")
            node, columns = self._compile_query(cte.this, out)
            declared = cte.args.get("alias")
            col_list = declared.args.get("columns") if declared else None
            if col_list:
                if len(col_list) != len(columns):
                    raise BuildError(
                        f"CTE {name!r} declares {len(col_list)} column name(s) but"
                        f" its query produces {len(columns)}"
                    )
                columns = [
                    OutColumn(
                        name=ident.name,
                        affinity=col.affinity,
                        collation=col.collation,
                        has_affinity=col.has_affinity,
                    )
                    for ident, col in zip(col_list, columns)
                ]
            out[ascii_fold(name)] = (node, columns)
        return out

    # -- SELECT -----------------------------------------------------------

    def _compile_select(self, select: exp.Select, ctes: dict):
        local_ctes = ctes
        with_clause = select.args.get("with_")
        if with_clause is not None:
            local_ctes = self._compile_with(with_clause, ctes)
        items, joined_node = self._compile_from(select, local_ctes)
        scope = Scope(items)
        # WHERE first (so annotations are filtered before projection).
        where = select.args.get("where")
        if where is not None:
            renderer = Renderer(scope, self.source_sql)
            predicate = renderer.bind_general(where.this, "WHERE")
            joined_node = Filter(joined_node, predicate)
        columns, expressions = self._projection(select, items, scope)
        projected = Project(joined_node, expressions, columns)
        return projected, columns

    def _compile_from(self, select: exp.Select, ctes: dict):
        from_clause = select.args.get("from_")
        joins = list(select.args.get("joins") or [])
        if from_clause is None:
            # No FROM: constant query; JOINs without a FROM are not valid SQL.
            if joins:
                raise UnsupportedQueryError("JOIN without FROM is not supported")
            return [], Const()
        items: list[ScopeItem] = []
        first = self._compile_source(from_clause.this, ctes)
        items.append(first)
        node: PlanNode = first.node
        for join in joins:
            right = self._compile_source(join.this, ctes)
            on = join.args.get("on")
            if on is None:
                # sqlglot inserts TRUE for INNER JOIN ... (no ON) and CROSS JOIN.
                node = CrossJoin(node, right.node)
            else:
                # Resolve ON against the columns visible so far *plus* the
                # right item.  Split the visible columns back into the two
                # physical stage tables so predicates can be evaluated as a
                # real two-table join: left items point at stage "l", the
                # right item at stage "r".
                qualifiers = {}
                for item in items:
                    qualifiers[id(item)] = "l"
                qualifiers[id(right)] = "r"
                renderer = Renderer(Scope(items + [right]), self.source_sql,
                                    qualifiers=qualifiers)
                predicate = renderer.bind_general(on, "JOIN ON")
                node = Join(node, right.node, predicate)
            items.append(right)
        return items, node

    def _compile_source(self, source, ctes: dict) -> ScopeItem:
        if isinstance(source, exp.Table):
            return self._compile_table(source, ctes)
        if isinstance(source, exp.Subquery):
            body = source.this
            node, columns = self._compile_query(body, ctes)
            alias = source.alias or ""
            declared = source.args.get("alias")
            col_list = declared.args.get("columns") if declared else None
            if col_list:
                if len(col_list) != len(columns):
                    raise BuildError(
                        f"derived table {alias!r} declares {len(col_list)} column"
                        f" name(s) but produces {len(columns)}"
                    )
                columns = [
                    OutColumn(name=ident.name, affinity=col.affinity,
                              collation=col.collation,
                              has_affinity=col.has_affinity)
                    for ident, col in zip(col_list, columns)
                ]
            if not alias:
                alias = ""
            return ScopeItem(alias, columns, node, base_table=None)
        raise UnsupportedQueryError(f"unsupported FROM item {type(source).__name__}")

    def _compile_table(self, table: exp.Table, ctes: dict) -> ScopeItem:
        name = table.name
        alias = table.alias or name
        declared = table.args.get("alias")
        col_list = declared.args.get("columns") if declared else None
        if ascii_fold(name) in ctes:
            if col_list:
                raise BuildError(
                    f"Cannot rename CTE {name!r} columns at a reference site;"
                    " declare the column list on the CTE definition"
                )
            node, columns = ctes[ascii_fold(name)]
            if not alias:
                alias = ""
            return ScopeItem(alias, columns, node, base_table=None)
        if col_list:
            raise BuildError(
                f"Cannot rename base table {name!r} column(s) at a reference site"
            )
        base = self.catalog.table(name)
        node = BaseScan(base, alias)
        return ScopeItem(alias, list(base.columns), node, base_table=name)

    # -- projection -------------------------------------------------------

    def _projection(self, select: exp.Select, items, scope: Scope):
        renderer = Renderer(scope, self.source_sql)
        columns: list[OutColumn] = []
        expressions: list[str] = []
        used_names: list[str] = []
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                for item in items:
                    for col in item.columns:
                        columns.append(_clone_column(col))
                        expressions.append(
                            renderer.raw_passthrough(items, item, col)
                        )
                        used_names.append(col.name)
                continue
            if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                qualifier = projection.args.get("table")
                qual = qualifier.name if qualifier is not None else None
                if qual is None:
                    raise BuildError("malformed qualified star")
                matched = False
                for item in items:
                    if item.alias == qual:
                        matched = True
                        for col in item.columns:
                            columns.append(_clone_column(col))
                            expressions.append(
                                renderer.raw_passthrough(items, item, col)
                            )
                            used_names.append(col.name)
                if not matched:
                    raise BuildError(f"unknown table or alias {qual!r} in {qual}.*")
                continue
            col = self._projection_column(projection, columns, used_names, scope)
            columns.append(col)
            bound = renderer.bind(_projection_expr(projection), "SELECT")
            expressions.append(_read_expression(bound))
            used_names.append(col.name)
        if not columns:
            raise UnsupportedQueryError(
                "the query produces no output columns; a result needs at least one"
            )
        return columns, expressions

    def _projection_column(self, projection, previous_columns, used_names,
                           scope) -> OutColumn:
        if isinstance(projection, exp.Alias):
            expr = projection.this
            name = projection.alias
        else:
            expr = projection
            name = _implicit_name(expr)
        if name is None or name == "":
            raise BuildError(
                "every output column needs a unique, non-empty name;"
                " give non-column expressions an explicit AS alias"
            )
        if name in used_names:
            raise BuildError(f"duplicate output column name {name!r}")
        affinity, collation, has_affinity = projected_metadata(
            expr, scope, self.source_sql
        )
        return OutColumn(
            name=name, affinity=affinity, collation=collation,
            has_affinity=has_affinity,
        )

    # -- UNION ALL --------------------------------------------------------

    def _compile_union(self, union: exp.Union, ctes: dict):
        local_ctes = ctes
        with_clause = union.args.get("with_")
        if with_clause is not None:
            # SQLite attaches a WITH clause to the whole compound SELECT; it is
            # visible to every branch and declared in order.
            local_ctes = self._compile_with(with_clause, ctes)
        left_node, columns = self._compile_query(union.this, local_ctes)
        right_node, columns2 = self._compile_query(
            union.args["expression"], local_ctes
        )
        if len(columns) != len(columns2):
            raise BuildError(
                "UNION ALL branches have different numbers of columns:"
                f" {len(columns)} vs {len(columns2)}"
            )
        _require_consistent_union_affinity(columns, columns2)
        return UnionAll([left_node, right_node], columns), columns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _affinity_signature(column: OutColumn) -> tuple:
    return (column.affinity, column.has_affinity, column.collation.name)


def _require_consistent_union_affinity(left: list[OutColumn], right: list[OutColumn]) -> None:
    """Reject a UNION ALL column whose mixed affinities are indeterminate.

    SQLite documents (datatypes.html section 3.3.1) that when a compound
    SELECT's corresponding result columns have different affinities *which*
    affinity applies "is indeterminate" and "might be different at different
    times within the same query".  A provenance tool cannot reproduce an
    undefined comparison rule, so a position whose branches disagree on
    affinity or collation is refused rather than guessed.
    """
    for index, (a, b) in enumerate(zip(left, right)):
        if _affinity_signature(a) != _affinity_signature(b):
            raise UnsupportedQueryError(
                f"UNION ALL column {index + 1} ({a.name!r}) mixes affinities"
                f" ({a.affinity}" + ("" if a.has_affinity else ", no affinity")
                + f") and ({b.affinity}" + ("" if b.has_affinity else ", no affinity")
                + ") with collations"
                f" {a.collation.name} / {b.collation.name}. SQLite leaves the"
                " result affinity indeterminate when compound branches disagree,"
                " so this query has no single well-defined comparison rule."
            )


def _clone_column(col: OutColumn) -> OutColumn:
    return OutColumn(
        name=col.name,
        affinity=col.affinity,
        collation=col.collation,
        base_table=col.base_table,
        base_column=col.base_column,
        has_affinity=col.has_affinity,
    )


def _read_expression(bound: Bound) -> str:
    return bound.raw_sql if bound.raw_sql is not None else bound.sql


def _projection_expr(projection: exp.Expression) -> exp.Expression:
    return projection.this if isinstance(projection, exp.Alias) else projection


def _implicit_name(expr: exp.Expression) -> Optional[str]:
    core = expr
    while isinstance(core, exp.Paren):
        core = core.this
    if isinstance(core, exp.Column):
        return core.name
    if isinstance(core, exp.Identifier):
        return core.name
    return None


def _has_leading_unary_plus(node: exp.Expression, source_sql: str) -> bool:
    from .render import unary_plus_positions, _leading_start

    if not source_sql:
        return False
    positions = unary_plus_positions(source_sql)
    if not positions:
        return False
    start = _leading_start(node)
    if start is None:
        return False
    probe = start - 1
    while probe >= 0 and source_sql[probe] in " \t\r\n(":
        probe -= 1
    return probe in positions


def projected_metadata(expr: exp.Expression, scope: Scope, source_sql: str = ""):
    """Affinity and collation of a projection expression (SQLite's rules).

    * a bare column (through parentheses) keeps its table column's affinity
      and collation;
    * ``CAST(expr AS type)`` has the affinity implied by the type name as
      written (SQLite applies affinity on the cast result);
    * an explicit ``COLLATE`` on top sets the collation and, per SQLite's
      rule, takes its affinity from its left operand;
    * everything else has BLOB (no) affinity and BINARY collation;
    * a leading unary ``+`` (dropped by the parser) turns a column reference
      into an expression with no affinity while keeping its collation.

    Note: a *compound* SELECT has an indeterminate affinity per SQLite's own
    documentation; the first branch is used here, which matches the planner's
    observations for all supported cases.
    """
    core = expr
    while isinstance(core, exp.Paren):
        core = core.this
    if isinstance(core, exp.Collate):
        affinity, _coll, has_affinity = projected_metadata(core.this, scope, source_sql)
        target = core.expression
        if isinstance(target, exp.Var):
            name = target.name.upper()
        elif isinstance(target, exp.Column):
            name = target.name.upper()
        else:
            name = "BINARY"
        if name not in ("BINARY", "NOCASE", "RTRIM"):
            raise UnsupportedQueryError(f"unsupported collation {name!r}")
        return affinity, Collation(name), has_affinity
    if isinstance(core, exp.Column):
        table = core.args.get("table")
        table_name = table.name if table is not None else None
        found = scope.resolve(core.name, table_name)
        if found is None:
            if table_name is not None:
                raise BuildError(
                    f"unknown column {table_name}.{core.name}; the qualifier"
                    " does not match any visible table alias"
                )
            raise BuildError(f"unknown column {core.name!r}")
        item, idx = found
        col = item.columns[idx]
        if _has_leading_unary_plus(core, source_sql):
            # ``+col`` is an expression, not a column reference: no affinity,
            # but the collation of the underlying column still applies.
            return AFF_BLOB, col.collation, False
        return col.affinity, col.collation, col.has_affinity
    if isinstance(core, exp.Cast):
        from .cols import affinity_for_declared_type
        from .render import _cast_type_text

        return (
            affinity_for_declared_type(_cast_type_text(core, source_sql)),
            Collation("BINARY"),
            True,
        )
    return AFF_BLOB, Collation("BINARY"), False
