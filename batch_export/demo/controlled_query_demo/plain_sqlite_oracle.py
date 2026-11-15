"""Independent oracle for the controlled-query demo.

This script is deliberately *not* part of the controlled_query package and
imports nothing from it.  It takes the hand-written expected rows from
``expectations.py``, loads them into a small in-memory SQLite database with
the same declared column types the gateway stages, and answers the demo
queries with plain SQLite.  The demo then proves that the service returns
exactly these answers for each identity.

Run standalone to print the expected answers:

    python3 -I demo/controlled_query_demo/plain_sqlite_oracle.py
"""
from __future__ import annotations

import base64
import os
import sqlite3
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import expectations as exp  # noqa: E402


def typed_key(value):
    """A stable, printable key for one SQLite value (NULL/int/real/text/
    blob stay distinct; integers keep full precision)."""
    if value is None:
        return "n"
    if isinstance(value, int):
        return "i:" + str(value)
    if isinstance(value, float):
        return "f:" + struct.pack(">d", value).hex()
    if isinstance(value, str):
        return "t:" + value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "b:" + base64.b64encode(bytes(value)).decode("ascii")
    raise TypeError(type(value).__name__)


def display(value):
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "x'" + value.hex() + "'"
    return repr(value) if isinstance(value, str) else str(value)


def build_authorized_db(rows_by_relation):
    """Create the small SQLite database holding exactly the expected
    authorized rows, with the same declared types the gateway stages."""
    conn = sqlite3.connect(":memory:")
    for relation, rows in rows_by_relation.items():
        shape = exp.RELATION_SHAPES[relation]
        definitions = ", ".join(f'"{name}" {decl}' for name, decl in shape)
        conn.execute(f'CREATE TABLE "{relation}" ({definitions})')
        if rows:
            placeholders = ", ".join("?" for _ in shape)
            conn.executemany(
                f'INSERT INTO "{relation}" VALUES ({placeholders})', rows)
    return conn


def answer(identity, sql):
    """The expected (columns, rows) for one identity and one query."""
    conn = build_authorized_db(exp.EXPECTED[identity])
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchall()
        return columns, rows
    finally:
        conn.close()


def main():
    for name, (sql, identities, _ordered) in exp.QUERIES.items():
        print(f"== {name} ==")
        print(f"   {sql}")
        for identity in identities:
            columns, rows = answer(identity, sql)
            print(f"   [{identity}] columns={columns}")
            for row in rows:
                print("      " + " | ".join(display(v) for v in row))
            if not rows:
                print("      (no rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
