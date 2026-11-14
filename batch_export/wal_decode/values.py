"""Machine-readable encoding of SQLite storage values.

The change stream must keep the five SQLite storage classes distinguishable --
in particular NULL vs empty TEXT vs a zero-length BLOB -- and must not lose
64-bit integer precision through JSON. Every value is therefore an explicit
tagged object::

    {"type": "null",   "value": null}
    {"type": "integer","value": "9223372036854775807"}   # decimal string
    {"type": "real",   "value": 3.14}
    {"type": "text",   "value": "abc"}                    # UTF-8 str
    {"type": "blob",   "value": "Zm9v", "encoding": "base64"}

High-precision numbers stored as TEXT remain TEXT (their string form is
preserved verbatim); BLOBs round-trip via standard base64. Integer values are
decimal strings so any JSON consumer retains the full 64-bit range; Python
callers also receive the native int on the event objects before serialisation.
"""
from __future__ import annotations

import base64
import json
from typing import Any

# sqlite3 module type codes
SQLITE_NULL = 0
SQLITE_INTEGER = 1
SQLITE_FLOAT = 2
SQLITE_TEXT = 3
SQLITE_BLOB = 4


def encode_value(value: Any) -> dict:
    """Encode a Python value as returned by sqlite3 into a tagged record."""
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):  # guard: sqlite never returns bool, stay strict
        raise TypeError(f"unexpected Python bool from SQLite: {value!r}")
    if isinstance(value, int):
        # Decimal string: JSON numbers cannot safely carry the full int64 range.
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "real", "value": value}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "type": "blob",
            "value": base64.b64encode(bytes(value)).decode("ascii"),
            "encoding": "base64",
        }
    raise TypeError(f"unsupported Python value type from SQLite: {type(value)!r}")


def decode_value(record: dict) -> Any:
    """Inverse of :func:`encode_value`, used by independent applier examples."""
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
    raise ValueError(f"unknown encoded storage type: {kind!r}")


class _TaggedEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (bytes, bytearray)):
            return encode_value(obj)
        return super().default(obj)


def dumps_json(obj: Any) -> str:
    """Stable JSON serialisation used by the CLI (ensure_ascii keeps UTF-8)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=False, separators=(",", ":"),
                      cls=_TaggedEncoder)
