"""Shared helpers for provenance-query tests.

Independent expectations come from plain SQLite: the same SQL is executed
directly against a copy of the warehouse, and rows are turned into a typed
multiset with Python (never through the provenance engine).  Exclusion
oracles delete the named base rows from a *second copy* and re-run the SQL.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from provenance_query import (  # noqa: E402
    ProvenanceQueryBuilder,
    ProvenanceResultReader,
    TyValue,
    evaluate_exclusion,
)


def result_multiset(reader: ProvenanceResultReader) -> dict:
    """Typed multiset of stored result tuples with their multiplicities."""
    out: dict = {}
    for row in reader.rows():
        key = tuple(v.key() for v in row.values)
        out[key] = out.get(key, 0) + row.multiplicity
    return out


def typed_multiset(rows) -> dict:
    """Typed multiset from cursor rows: typed value tuple -> count."""
    multiset: dict = {}
    for row in rows:
        key = tuple(TyValue.from_sqlite(v).key() for v in row)
        multiset[key] = multiset.get(key, 0) + 1
    return multiset


def decode_tuple(row_values) -> tuple:
    return tuple(v.key() for v in row_values)


class WarehouseTestCase(unittest.TestCase):
    """A temp warehouse, a SQL file helper and an independent-oracle helper."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pq_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = os.path.join(self.tmp, "warehouse.sqlite")
        self.result_dir = os.path.join(self.tmp, "results")
        os.makedirs(self.result_dir, exist_ok=True)

    # -- warehouse --------------------------------------------------------

    def make_warehouse(self, script: str) -> str:
        # Rebuild from scratch so repeated calls cannot collide on existing
        # tables; each call defines a fresh warehouse.
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(script)
            conn.commit()
        finally:
            conn.close()
        return self.db_path

    def copy_warehouse(self, name: str = "copy.sqlite") -> str:
        path = os.path.join(self.tmp, name)
        shutil.copyfile(self.db_path, path)
        return path

    # -- SQL file and build ----------------------------------------------

    def write_sql(self, sql: str, name: str = "query.sql") -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(sql)
        return path

    def build(self, sql: str, name: str = "result.sqlite") -> str:
        sql_path = self.write_sql(sql, name.replace(".sqlite", "") + ".sql")
        result_path = os.path.join(self.result_dir, name)
        if os.path.exists(result_path):
            os.remove(result_path)
        ProvenanceQueryBuilder(self.db_path).build(sql_path, result_path)
        return result_path

    def reader(self, result_path: str) -> ProvenanceResultReader:
        reader = ProvenanceResultReader(result_path)
        self.addCleanup(reader.close)
        return reader

    def result_multiset(self, reader: ProvenanceResultReader) -> dict:
        return result_multiset(reader)

    # -- independent oracles ---------------------------------------------

    def direct_multiset(self, sql: str, db_path: str = None) -> dict:
        """Run the same SQL in plain SQLite and type each value in Python."""
        path = db_path or self.copy_warehouse("oracle.sqlite")
        conn = sqlite3.connect(path)
        conn.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
        try:
            cursor = conn.execute(sql)
            return typed_multiset(cursor.fetchall())
        finally:
            conn.close()

    def deletion_multiset(self, sql: str, deletions: dict) -> dict:
        """Delete base rows in a copy and re-run the SQL directly.

        ``deletions`` maps ``"TABLE" -> [pk sqlite values]`` where each entry
        is a tuple of native Python/SQLite values in declared PK order.
        """
        path = self.copy_warehouse("deleted.sqlite")
        conn = sqlite3.connect(path)
        conn.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
        try:
            for table, rows in deletions.items():
                conn.executemany(
                    f'DELETE FROM "{table}" WHERE ' + " AND ".join(
                        f'"{col}" = ?' for col in _pk_columns(conn, table)
                    ),
                    rows,
                )
            conn.commit()
            cursor = conn.execute(sql)
            return typed_multiset(cursor.fetchall())
        finally:
            conn.close()

    def engine_exclusion_multiset(self, result_path: str, identities) -> dict:
        outcome = evaluate_exclusion(result_path, identities)
        out: dict = {}
        for row in outcome["rows"]:
            if row["after"] == 0:
                continue
            key = tuple(_decode_encoded(v) for v in row["values"])
            out[key] = out.get(key, 0) + row["after"]
        return out

    # -- assertions -------------------------------------------------------

    def assert_multisets_equal(self, actual: dict, expected: dict, msg=""):
        self.assertEqual(
            actual, expected,
            msg or f"typed multisets differ:\n actual={sorted(actual.items())}"
                  f"\n expected={sorted(expected.items())}",
        )


def _pk_columns(conn, table: str) -> list[str]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] > 0]


def _decode_encoded(value) -> str:
    if isinstance(value, dict):
        return TyValue.decode(value).key()
    return value
