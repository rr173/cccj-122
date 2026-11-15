"""Runtime for staged SQLite evaluation and provenance-aware relational nodes.

A *stage* is a private TEMP table with, per visible column, a raw value column
``v<i>`` and a generated affinity column ``g<i>``.  Staging lets SQLite itself
evaluate filters, projections and join predicates, so affinity, collation,
coercion and three-valued logic match SQLite exactly.  The provenance
annotation travels in ``__ann`` and is opaque to SQLite.

Every node collapses duplicate typed output tuples and sums their annotations
before exposing rows, so intermediate Cartesian products stay compact.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .cols import OutColumn, ProvRow
from .graph import ProvenanceGraph
from .stage import declared_type_for
from .values import TyValue, tuple_key


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Env:
    """Evaluation context: one private SQLite connection and one graph."""

    def __init__(self, conn, graph: ProvenanceGraph):
        self.conn = conn
        self.graph = graph
        self._counter = 0

    def name(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter}"

    def create_stage(self, name: str, columns: Sequence[OutColumn]) -> "Stage":
        return Stage(self.conn, name, columns)


class Stage:
    """A private temp table mirroring one relation's rows."""

    __slots__ = ("conn", "qualified", "columns", "annotations", "_pending")

    def __init__(self, conn, name: str, columns: Sequence[OutColumn]):
        self.conn = conn
        self.qualified = quote(name)
        self.columns = list(columns)
        self.annotations: list[int] = []
        self._pending: list[tuple] = []
        defs = ['"__ann" TEXT NOT NULL']
        for i, col in enumerate(columns):
            defs.append(quote(f"v{i}"))
            decl = declared_type_for(col)
            gen = f"{quote('g' + str(i))} {decl} GENERATED ALWAYS AS ({quote('v' + str(i))}) VIRTUAL".strip()
            if not col.collation.is_binary():
                gen += f" COLLATE {col.collation.name}"
            defs.append(gen)
        conn.execute(f"CREATE TEMP TABLE {self.qualified} ({', '.join(defs)})")

    def add(self, values: Sequence[TyValue], annotation: int) -> None:
        self.annotations.append(annotation)
        self._pending.append((str(annotation), *[v.value for v in values]))
        if len(self._pending) >= 500:
            self.flush()

    def add_all(self, rows: Iterable[ProvRow]) -> None:
        for row in rows:
            self.add(row.values, row.annotation)

    def flush(self) -> None:
        if not self._pending:
            return
        width = 1 + len(self.columns)
        holders = ", ".join("?" for _ in range(width))
        self.conn.executemany(
            f"INSERT INTO {self.qualified} VALUES ({holders})", self._pending
        )
        self._pending = []

    def finish(self) -> None:
        self.flush()

    def rowids_where(self, predicate_sql: str) -> list[tuple[int, tuple]]:
        """Rows satisfying the predicate: ``(rowid, raw value tuple)``."""
        select_list = self._raw_select_list()
        prefix = f"{select_list}, " if select_list else ""
        cur = self.conn.execute(
            f"SELECT rowid, {prefix}1 FROM {self.qualified}"
            f" WHERE ({predicate_sql}) ORDER BY rowid"
        )
        if select_list:
            return [(row[0], tuple(row[1:-1])) for row in cur]
        return [(row[0], tuple()) for row in cur]

    def _raw_select_list(self) -> str:
        return ", ".join(quote(f"v{i}") for i in range(len(self.columns)))

    def project(self, expressions: Sequence[str]) -> list[tuple]:
        cols = ", ".join(
            f"({expr}) AS {quote('p' + str(i))}" for i, expr in enumerate(expressions)
        )
        return self.conn.execute(
            f"SELECT rowid, {cols} FROM {self.qualified} ORDER BY rowid"
        ).fetchall()

    def annotation(self, rowid: int) -> int:
        return self.annotations[rowid - 1]

    def drop(self) -> None:
        try:
            self.conn.execute(f"DROP TABLE IF EXISTS {self.qualified}")
        except Exception:  # pragma: no cover - defensive
            pass


def collapse(rows: Iterable[ProvRow], graph: ProvenanceGraph) -> list[ProvRow]:
    """Collapse duplicate typed tuples, summing annotations, stable order."""
    values: list[tuple] = []
    anns: list[int] = []
    index: dict[tuple, int] = {}
    for row in rows:
        key = tuple_key(row.values)
        pos = index.get(key)
        if pos is None:
            index[key] = len(values)
            values.append(row.values)
            anns.append(row.annotation)
        else:
            anns[pos] = graph.add(anns[pos], row.annotation)
    return [ProvRow(v, a) for v, a in zip(values, anns)]


# ---------------------------------------------------------------------------
# Plan nodes
# ---------------------------------------------------------------------------


class PlanNode:
    columns: list[OutColumn]
    label = "node"

    def __init__(self) -> None:
        self._rows: Optional[list[ProvRow]] = None

    def compute(self, env: Env) -> list[ProvRow]:  # pragma: no cover - abstract
        raise NotImplementedError

    def rows(self, env: Env) -> list[ProvRow]:
        if self._rows is None:
            self._rows = collapse(self.compute(env), env.graph)
        return self._rows


class BaseScan(PlanNode):
    """One token per stored base row, in declared primary-key order."""

    def __init__(self, table, alias: str):
        super().__init__()
        self.table = table
        self.alias = alias
        self.columns = list(table.columns)
        self.label = f"scan({table.name})"

    def compute(self, env: Env) -> list[ProvRow]:
        names = [c.name for c in self.table.columns]
        pk_positions = [names.index(pk) for pk in self.table.pk_columns]
        cols = ", ".join(quote(n) for n in names)
        order = ", ".join(quote(p) for p in self.table.pk_columns)
        cursor = env.conn.execute(
            f"SELECT {cols} FROM {quote(self.table.name)} ORDER BY {order}"
        )
        out: list[ProvRow] = []
        for raw in cursor:
            values = tuple(TyValue.from_sqlite(v) for v in raw)
            pk_values = [values[i] for i in pk_positions]
            token = env.graph.universe.token(
                self.table.name, self.table.name, self.table.pk_columns, pk_values
            )
            out.append(ProvRow(values, env.graph.token(token)))
            env.graph.universe.note_row_loaded(self.table.name)
        return out


class Const(PlanNode):
    """No-FROM SELECT: one zero-column row with annotation 1."""

    def __init__(self) -> None:
        super().__init__()
        self.columns = []
        self.label = "const"

    def compute(self, env: Env) -> list[ProvRow]:
        return [ProvRow((), env.graph.one)]


class Filter(PlanNode):
    """Keep rows whose predicate is TRUE; annotations unchanged."""

    def __init__(self, child: PlanNode, predicate_sql: str):
        super().__init__()
        self.child = child
        self.predicate_sql = predicate_sql
        self.columns = list(child.columns)
        self.label = "filter"

    def compute(self, env: Env) -> list[ProvRow]:
        stage = env.create_stage(env.name("filter"), self.child.columns)
        stage.add_all(self.child.rows(env))
        stage.finish()
        kept = stage.rowids_where(self.predicate_sql)
        out = [
            ProvRow(tuple(TyValue.from_sqlite(v) for v in raw), stage.annotation(rowid))
            for rowid, raw in kept
        ]
        stage.drop()
        return out


class Project(PlanNode):
    """Evaluate output expressions per row; annotations unchanged."""

    def __init__(self, child: PlanNode, expressions: list[str], columns: list[OutColumn]):
        super().__init__()
        self.child = child
        self.expressions = expressions
        self.columns = columns
        self.label = "project"

    def compute(self, env: Env) -> list[ProvRow]:
        stage = env.create_stage(env.name("project"), self.child.columns)
        stage.add_all(self.child.rows(env))
        stage.finish()
        out: list[ProvRow] = []
        for raw in stage.project(self.expressions):
            rowid = raw[0]
            values = tuple(TyValue.from_sqlite(v) for v in raw[1:])
            out.append(ProvRow(values, stage.annotation(rowid)))
        stage.drop()
        return out


class CrossJoin(PlanNode):
    """Cartesian product; annotation = left * right.

    Rows are collapsed incrementally while the product is built, so a product
    that maps many pairs onto the same typed tuple (the compactness case)
    never materialises the pair list: only distinct tuples are retained.
    """

    def __init__(self, left: PlanNode, right: PlanNode):
        super().__init__()
        self.left = left
        self.right = right
        self.columns = list(left.columns) + list(right.columns)
        self.label = "cross"

    def compute(self, env: Env) -> list[ProvRow]:
        from .values import tuple_key

        left_rows = self.left.rows(env)
        right_rows = self.right.rows(env)
        accumulator: dict[tuple, int] = {}
        order: list[tuple] = []
        values_by_key: dict[tuple, tuple] = {}
        for lrow in left_rows:
            for rrow in right_rows:
                values = lrow.values + rrow.values
                annotation = env.graph.mul(lrow.annotation, rrow.annotation)
                key = tuple_key(values)
                previous = accumulator.get(key)
                if previous is None:
                    accumulator[key] = annotation
                    order.append(key)
                    values_by_key[key] = values
                else:
                    accumulator[key] = env.graph.add(previous, annotation)
        return [ProvRow(values_by_key[key], accumulator[key]) for key in order]


class Join(PlanNode):
    """INNER JOIN with an ON predicate evaluated by SQLite."""

    def __init__(self, left: PlanNode, right: PlanNode, predicate_sql: str):
        super().__init__()
        self.left = left
        self.right = right
        self.predicate_sql = predicate_sql
        self.columns = list(left.columns) + list(right.columns)
        self.label = "join"

    def compute(self, env: Env) -> list[ProvRow]:
        left_rows = self.left.rows(env)
        right_rows = self.right.rows(env)
        lstage = env.create_stage(env.name("join_l"), self.left.columns)
        rstage = env.create_stage(env.name("join_r"), self.right.columns)
        lstage.add_all(left_rows)
        rstage.add_all(right_rows)
        lstage.finish()
        rstage.finish()
        sql = (
            f"SELECT {quote('l')}.rowid, {quote('r')}.rowid"
            f" FROM {lstage.qualified} AS {quote('l')}"
            f" JOIN {rstage.qualified} AS {quote('r')} ON ({self.predicate_sql})"
            f" ORDER BY {quote('l')}.rowid, {quote('r')}.rowid"
        )
        out: list[ProvRow] = []
        for lid, rid in env.conn.execute(sql):
            out.append(
                ProvRow(
                    left_rows[lid - 1].values + right_rows[rid - 1].values,
                    env.graph.mul(lstage.annotation(lid), rstage.annotation(rid)),
                )
            )
        lstage.drop()
        rstage.drop()
        return out


class UnionAll(PlanNode):
    """UNION ALL: concatenate; duplicate collapsing happens in ``rows``."""

    def __init__(self, branches: list[PlanNode], columns: list[OutColumn]):
        super().__init__()
        self.branches = branches
        self.columns = columns
        self.label = "union"

    def compute(self, env: Env) -> list[ProvRow]:
        out: list[ProvRow] = []
        for branch in self.branches:
            out.extend(branch.rows(env))
        return out
