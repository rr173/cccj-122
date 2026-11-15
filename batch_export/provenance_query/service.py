"""Build a provenance-carrying query result from an existing SQLite warehouse.

Usage::

    builder = ProvenanceQueryBuilder(db_path)
    report = builder.build(sql_file, result_file)

The source database is opened read-only with ``mode=ro`` inside a single
transaction that covers catalog reads, base-row tokenisation and the whole
query evaluation, so rows from different commits can never be mixed.  All
working tables live in a private TEMP schema; nothing is written to the
source database (and no ``-wal``/``-shm``/journal is created beside it).

The finished result file is published atomically: a failed build either
removes its temporary file or leaves the target untouched, never a half-written
result.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote as url_quote

from .compile import Compiler
from .errors import BuildError, InvocationError, UnsupportedQueryError
from .resultfile import ResultWriter, utcnow, FORMAT_NAME, FORMAT_VERSION
from .runtime import Env
from .sqlparse import parse_sql_file

# Hard limits that keep pathological inputs from exhausting the process.
_MAX_RESULT_ROWS = 20_000_000


@dataclass
class BuildReport:
    result_path: str
    query_text: str
    result_rows: int
    original_multiplicity_total: int
    graph_nodes: int
    graph_edges: int
    tokens: int
    value_columns: int
    elapsed_seconds: float


def read_only_uri(path: Path) -> str:
    """A SQLite read-only URI that is safe for spaces and special characters.

    ``urllib.parse.quote`` with an empty safe set encodes ``/`` too, which is
    what SQLite's URI parser expects (it decodes percent escapes itself).
    """
    encoded = url_quote(path.resolve().as_posix(), safe="")
    return f"file:{encoded}?mode=ro"


class ProvenanceQueryBuilder:
    """Build one provenance result file from one SQL file."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def _open_source(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        if not path.is_file():
            raise InvocationError(f"source database not found: {self.db_path}")
        uri = read_only_uri(path)
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        except sqlite3.Error as exc:
            raise InvocationError(f"cannot open source database read-only: {exc}") from None
        conn.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
        return conn

    def build(self, sql_path: str, result_path: str, *, overwrite: bool = False) -> BuildReport:
        target = Path(result_path)
        if target.exists() and not overwrite:
            raise BuildError(
                f"result file {result_path} already exists; refusing to overwrite"
            )
        tree, sql_text = parse_sql_file(sql_path)
        started = time.monotonic()
        source = self._open_source()
        writer: Optional[ResultWriter] = None
        try:
            source.execute("BEGIN")
            compiler = Compiler(source, sql_text)
            plan = compiler.compile(tree)
            env = Env(source, compiler.graph)
            rows = plan.root.rows(env)
            if len(rows) > _MAX_RESULT_ROWS:
                raise BuildError(
                    f"the result has {len(rows)} distinct tuples, above the"
                    f" supported limit {_MAX_RESULT_ROWS}"
                )
            evaluated = compiler.graph.evaluate_many(row.annotation for row in rows)
            total = sum(evaluated[row.annotation] for row in rows)
            writer = ResultWriter(str(target))
            writer.set_meta(
                format=FORMAT_NAME,
                format_version=FORMAT_VERSION,
                built_at=utcnow(),
                query_text=sql_text,
                source_path=str(Path(self.db_path).resolve()),
                value_columns=len(plan.columns),
                result_rows=len(rows),
                original_multiplicity_total=str(total),
                graph_nodes=compiler.graph.node_count,
                graph_edges=compiler.graph.edge_count,
                tokens=len(compiler.graph.universe.tokens),
            )
            writer.set_columns(plan.columns)
            for ordinal, row in enumerate(rows):
                writer.add_row(
                    ordinal, row.values,
                    evaluated[row.annotation], row.annotation,
                )
            for token in compiler.graph.universe.tokens:
                writer.add_term(token)
            writer.add_graph(compiler.graph)
            # Publish only after the snapshot transaction has been committed to
            # our own read view: the file either exists complete or not at all.
            source.execute("COMMIT")
            writer.publish()
            return BuildReport(
                result_path=str(target),
                query_text=sql_text,
                result_rows=len(rows),
                original_multiplicity_total=total,
                graph_nodes=compiler.graph.node_count,
                graph_edges=compiler.graph.edge_count,
                tokens=len(compiler.graph.universe.tokens),
                value_columns=len(plan.columns),
                elapsed_seconds=time.monotonic() - started,
            )
        except InvocationError:
            raise
        except UnsupportedQueryError:
            if writer is not None:
                writer.abort()
            raise
        except BuildError:
            if writer is not None:
                writer.abort()
            raise
        except BaseException as exc:
            if writer is not None:
                writer.abort()
            raise BuildError(f"build failed: {exc}") from None
        finally:
            try:
                if source.in_transaction:
                    source.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            source.close()
