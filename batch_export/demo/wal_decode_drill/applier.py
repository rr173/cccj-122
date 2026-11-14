"""Independent row-event applier for the acceptance drill.

This module is deliberately separate from the production ``wal_decode``
package: it never imports it. It consumes the decoder's JSONL commit stream
plus SQL snapshots taken independently through ordinary read-only SQLite
connections, and verifies that applying the net changes reproduces *every*
intermediate committed state -- not just the final row count.

State representation: ``{table: {identity_tuple: {column: encoded_value}}}``.
Composite identities are tuples of decoded scalars, never concatenated
strings; BLOBs round-trip through the tagged base64 encoding.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from typing import Any


def decode_scalar(record: dict) -> Any:
    kind = record["type"]
    if kind == "null":
        return None
    if kind == "integer":
        return int(record["value"])
    if kind == "real":
        return float(record["value"])
    if kind == "text":
        return record["value"]
    if kind == "blob":
        return base64.b64decode(record["value"])
    raise ValueError(f"unknown storage type {kind!r}")


def sql_snapshot(db_path: str, identity_columns: dict[str, list[str]]) -> dict:
    """Independent ground truth through plain SQLite (immutable, read-only).

    All stored columns are read (discovered via PRAGMA table_info, matching the
    decoder's full-row images), but rows are keyed only by the identity
    columns -- never by the whole row and never by a concatenated string.
    """
    uri = "file:" + os.path.abspath(db_path).replace("?", "%3f") + "?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        result = {}
        for table, id_columns in identity_columns.items():
            # table_info returns stored columns in table order; the 6th tuple
            # field is the PRIMARY KEY position (1-based), not a hidden flag.
            pragma = conn.execute(
                f'PRAGMA table_info("{table}")'
            ).fetchall()
            all_columns = [r[1] for r in pragma]
            id_positions = [all_columns.index(c) for c in id_columns]
            col_sql = ", ".join('"' + c + '"' for c in all_columns)
            rows = conn.execute(
                f"SELECT {col_sql} FROM \"{table}\""
            ).fetchall()
            bucket = {}
            for row in rows:
                values = list(row)
                identity = tuple(values[p] for p in id_positions)
                bucket[identity] = {
                    col: _raw_tagged(value)
                    for col, value in zip(all_columns, values)
                }
            result[table] = bucket
        return result
    finally:
        conn.close()


def _raw_tagged(value: Any) -> dict:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "real", "value": value}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob",
                "value": base64.b64encode(bytes(value)).decode("ascii"),
                "encoding": "base64"}
    raise TypeError(type(value))


def apply_commit(state: dict, commit: dict, identity_len: dict[str, int]) -> dict:
    """Apply one commit record's net changes onto the in-memory state."""
    new_state = {table: dict(rows) for table, rows in state.items()}
    for change in commit["changes"]:
        table = change["table"]
        id_len = identity_len[table]
        bucket = new_state.setdefault(table, {})
        for event in change["events"]:
            kind = event["kind"]
            if kind == "insert":
                identity = tuple(
                    decode_scalar(event["after"][col])
                    for col in event["identity_columns"]
                )
                assert identity not in bucket, \
                    f"insert of existing identity {identity} in {table}"
                bucket[identity] = event["after"]
            elif kind == "delete":
                identity = tuple(
                    decode_scalar(event["before"][col])
                    for col in event["identity_columns"]
                )
                assert identity in bucket, \
                    f"delete of missing identity {identity} in {table}"
                del bucket[identity]
            else:  # update
                identity = tuple(
                    decode_scalar(event["after"][col])
                    for col in event["identity_columns"]
                )
                old_identity = tuple(
                    decode_scalar(event["before"][col])
                    for col in event["identity_columns"]
                )
                assert identity == old_identity, \
                    "identity change must be expressed as delete+insert"
                assert identity in bucket
                # Full-row replacement using the complete after-image.
                bucket[identity] = event["after"]
    return new_state


def load_commits(jsonl_path: str) -> list[dict]:
    commits = []
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                commits.append(json.loads(line))
    return commits
