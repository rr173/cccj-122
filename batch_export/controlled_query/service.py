"""The controlled analytical query service.

One call runs as follows:

1. the caller-named identity is looked up in the policy (unknown identities
   are an invocation error) and its entity effects are resolved;
2. the SQL text is parsed and checked against the approved read-only
   subset, resolving table names against the catalog's public relations
   with a proper CTE scope;
3. the source database is opened read-only and a single read transaction
   begins -- in WAL mode this pins one consistent snapshot while writers
   keep committing;
4. inside that snapshot the catalog is validated against the live schema,
   every hop's target key is proved non-null and unique, and each public
   relation is materialized for this identity (owner resolution, grant
   application, summary masking);
5. the approved statement runs on a **private in-memory SQLite database**
   that contains only the staged authorized relations -- the physical
   tables, views, metadata and everyone else's rows simply do not exist in
   that execution environment;
6. the typed result is returned and, when asked, published atomically.

The source database is never written: no helper objects, no schema or data
changes, no journal/``-wal`` side files are created by this service.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote as url_quote

from provenance_query.values import TyValue

from .catalog import Catalog, load_catalog, quote_ident
from .errors import (
    ConfigError,
    InvocationError,
    QueryExecutionError,
)
from .policy import Policy, load_policy
from .resultfile import build_document, publish_result
from .sqlcheck import check_query
from .stage import StagedRelation, stage_all

# Hard limit that keeps pathological queries from exhausting the process.
_MAX_RESULT_ROWS = 5_000_000


@dataclass
class QueryReport:
    identity: str
    sql: str
    relations: list          # public relations the query actually used
    columns: list            # output column names
    rows: list               # [tuple[TyValue]]
    result_path: Optional[str] = None
    staged: list = field(default_factory=list)   # per-relation staging stats
    elapsed_seconds: float = 0.0

    def to_document(self) -> dict:
        return build_document(self)


@dataclass
class ConfigReport:
    relations: list  # [(name, source, fields, hops, hidden)]


class ControlledQueryService:
    """One service bound to a warehouse, a relation catalog and a policy."""

    def __init__(self, db_path: str, catalog_path: str, policy_path: str):
        path = Path(db_path)
        if not path.is_file():
            raise InvocationError(f"source database not found: {db_path}")
        self.db_path = str(path)
        self.catalog: Catalog = load_catalog(catalog_path)
        self.policy: Policy = load_policy(policy_path)

    # -- source snapshot ------------------------------------------------------

    def _open_source(self) -> sqlite3.Connection:
        encoded = url_quote(Path(self.db_path).resolve().as_posix(), safe="")
        try:
            conn = sqlite3.connect(
                f"file:{encoded}?mode=ro", uri=True, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise InvocationError(
                f"cannot open the source database read-only: {exc}"
            ) from None
        conn.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
        return conn

    def _snapshot_stage(self, effects: dict, snapshot_hook=None) -> list:
        """Validate the catalog and stage every relation inside one read
        transaction; returns the staged relations."""
        source = self._open_source()
        try:
            source.execute("BEGIN")
            resolved = self.catalog.validate_schema(source)
            if snapshot_hook is not None:
                # Testing hook: fires once the snapshot is firmly established
                # (schema reads already happened on it) and before any
                # business row is read.
                snapshot_hook()
            self.catalog.check_target_keys(source)
            staged = stage_all(source, resolved, effects)
            source.execute("COMMIT")
            return staged
        except (ConfigError, InvocationError):
            self._rollback_quietly(source)
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly(source)
            raise InvocationError(
                f"cannot read the source database: {exc}"
            ) from None
        except BaseException:
            self._rollback_quietly(source)
            raise
        finally:
            source.close()

    @staticmethod
    def _rollback_quietly(source) -> None:
        try:
            if source.in_transaction:
                source.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # -- config check ----------------------------------------------------------

    def check_config(self, snapshot_hook=None) -> ConfigReport:
        """Validate catalog + policy + live schema + target keys only."""
        self._snapshot_stage({}, snapshot_hook=snapshot_hook)
        return ConfigReport(relations=[
            (rel.name, rel.source, [f.name for f in rel.fields],
             [(h.table, list(h.left), list(h.right)) for h in rel.hops],
             sorted(rel.summary_hidden))
            for rel in self.catalog.relations
        ])

    # -- the query --------------------------------------------------------------

    def query(self, identity: str, sql: Optional[str] = None,
              sql_file: Optional[str] = None,
              result_path: Optional[str] = None, *,
              overwrite: bool = False, snapshot_hook=None) -> QueryReport:
        started = time.monotonic()
        ident = self.policy.identity(identity)  # unknown identity -> exit 2
        effects = self.policy.resolve(ident)
        sql_text = self._read_sql(sql, sql_file)
        checked = check_query(sql_text, self.catalog)

        staged = self._snapshot_stage(effects, snapshot_hook=snapshot_hook)

        private = sqlite3.connect(":memory:")
        private.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
        try:
            for relation in staged:
                _create_private_table(private, relation)
                _fill_private_table(private, relation)
            cursor = private.execute(checked.sql)
            columns = [description[0] for description in cursor.description or []]
            raw_rows = cursor.fetchall()
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise QueryExecutionError(
                f"the statement failed on the authorized relations: {exc}"
            ) from None
        finally:
            private.close()
        if len(raw_rows) > _MAX_RESULT_ROWS:
            raise QueryExecutionError(
                f"the result has {len(raw_rows)} rows, above the supported"
                f" limit {_MAX_RESULT_ROWS}"
            )
        rows = [tuple(TyValue.from_sqlite(value) for value in row)
                for row in raw_rows]
        report = QueryReport(
            identity=ident.name,
            sql=sql_text,
            relations=checked.relations,
            columns=columns,
            rows=rows,
            staged=staged,
            elapsed_seconds=time.monotonic() - started,
        )
        if result_path is not None:
            report.result_path = publish_result(
                result_path, report.to_document(), overwrite=overwrite
            )
        return report

    def _read_sql(self, sql: Optional[str], sql_file: Optional[str]) -> str:
        if (sql is None) == (sql_file is None):
            raise InvocationError("pass exactly one of sql / sql_file")
        if sql is not None:
            if not isinstance(sql, str):
                raise InvocationError("sql must be a string")
            return sql
        try:
            with open(sql_file, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise InvocationError(f"cannot read SQL file {sql_file}: {exc}") from None
        except UnicodeDecodeError as exc:
            raise InvocationError(
                f"SQL file {sql_file} is not valid UTF-8: {exc}"
            ) from None


# ---------------------------------------------------------------------------
# Private execution environment
# ---------------------------------------------------------------------------


def _create_private_table(conn: sqlite3.Connection,
                          relation: StagedRelation) -> None:
    """Create the public relation with exactly its published fields and the
    source columns' declared types, so SQLite applies the same type
    affinities it would on a hand-built small database of the same shape."""
    definitions = []
    for name, declared in relation.columns:
        if declared:
            definitions.append(f"{quote_ident(name)} {declared}")
        else:
            definitions.append(quote_ident(name))
    conn.execute(
        f"CREATE TABLE {quote_ident(relation.name)} ({', '.join(definitions)})"
    )


def _fill_private_table(conn: sqlite3.Connection,
                        relation: StagedRelation) -> None:
    if not relation.rows:
        return
    placeholders = ", ".join("?" for _ in relation.columns)
    conn.executemany(
        f"INSERT INTO {quote_ident(relation.name)} VALUES ({placeholders})",
        relation.rows,
    )
