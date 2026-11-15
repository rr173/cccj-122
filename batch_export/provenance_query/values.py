"""Typed SQLite values used for result equality and serialisation.

The result file must distinguish NULL, integer, real, text and BLOB; must not
lose integer precision to JSON; and must keep empty text distinct from empty
BLOB.  A :class:`TyValue` is a tiny immutable tagged value with a *stable*
key (used for grouping, equality and comparison) and a JSON form.

The key form is a single ``str``:

* ``n``                         NULL
* ``i:<decimal>``               integer (arbitrary precision kept as text)
* ``f:<16-hex-digits>``         real, stored as the IEEE-754 bit pattern so
                                NaN/Inf/-0.0 survive JSON round trips
* ``t:<text>``                  text (UTF-8, may be empty)
* ``b:<base64>``                blob (may be empty)

Keys sort in SQLite's own storage-class order (NULL < numbers < text < blob);
within numbers integers and reals are ordered numerically when sorting, but
they remain *distinct* keys (``1`` and ``1.0`` are different values here,
because they are different storage classes, just as ``typeof()`` differs).
"""
from __future__ import annotations

import base64
import math
import struct
from typing import Union

NULL = "n"
INTEGER = "i"
REAL = "f"
TEXT = "t"
BLOB = "b"

_KIND_ORDER = {NULL: 0, INTEGER: 1, REAL: 1, TEXT: 2, BLOB: 3}


class TyValue:
    """One SQLite value with an explicit storage class."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value):
        self.kind = kind
        self.value = value

    # -- constructors -----------------------------------------------------

    @classmethod
    def null(cls) -> "TyValue":
        return cls(NULL, None)

    @classmethod
    def integer(cls, value: int) -> "TyValue":
        if isinstance(value, bool):  # bool is an int subclass; keep it an int
            value = int(value)
        return cls(INTEGER, int(value))

    @classmethod
    def real(cls, value: float) -> "TyValue":
        return cls(REAL, float(value))

    @classmethod
    def text(cls, value: str) -> "TyValue":
        if not isinstance(value, str):
            raise TypeError(f"text value must be str, got {type(value).__name__}")
        return cls(TEXT, value)

    @classmethod
    def blob(cls, value: bytes) -> "TyValue":
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError(f"blob value must be bytes, got {type(value).__name__}")
        return cls(BLOB, bytes(value))

    @classmethod
    def from_sqlite(cls, value) -> "TyValue":
        """Convert one value returned by sqlite3 with ``text_factory=str``."""
        if value is None:
            return cls.null()
        if isinstance(value, bool):
            return cls.integer(int(value))
        if isinstance(value, int):
            return cls.integer(value)
        if isinstance(value, float):
            return cls.real(value)
        if isinstance(value, str):
            return cls.text(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return cls.blob(bytes(value))
        raise TypeError(f"unsupported SQLite value type {type(value).__name__}")

    # -- representation ---------------------------------------------------

    def key(self) -> str:
        if self.kind == NULL:
            return "n"
        if self.kind == INTEGER:
            return "i:" + str(self.value)
        if self.kind == REAL:
            bits = struct.pack(">d", self.value).hex()
            return "f:" + bits
        if self.kind == TEXT:
            return "t:" + self.value
        if self.kind == BLOB:
            return "b:" + base64.b64encode(self.value).decode("ascii")
        raise ValueError(f"unknown value kind {self.kind!r}")  # pragma: no cover

    def sort_key(self):
        """A key that compares like SQLite's ORDER BY on mixed values."""
        order = _KIND_ORDER[self.kind]
        if self.kind == INTEGER:
            return (order, 0, self.value)
        if self.kind == REAL:
            return (order, 1, self.value)
        if self.kind == NULL:
            return (order, 0, 0)
        return (order, 0, self.key())

    def encode(self) -> dict:
        """JSON-friendly form; integers stay decimal strings to avoid loss."""
        if self.kind == NULL:
            return {"t": "null"}
        if self.kind == INTEGER:
            return {"t": "int", "v": str(self.value)}
        if self.kind == REAL:
            # Always emit a JSON number: Python's json keeps repr() of a float,
            # which round-trips exactly for finite values.  Non-finite values
            # are emitted as explicit strings.
            if math.isfinite(self.value):
                return {"t": "real", "v": self.value}
            return {"t": "real", "v": repr(self.value)}
        if self.kind == TEXT:
            return {"t": "text", "v": self.value}
        if self.kind == BLOB:
            return {"t": "blob", "v": base64.b64encode(self.value).decode("ascii")}
        raise ValueError(f"unknown value kind {self.kind!r}")  # pragma: no cover

    @classmethod
    def decode(cls, obj: dict) -> "TyValue":
        kind = obj.get("t")
        if kind == "null":
            return cls.null()
        if kind == "int":
            raw = obj.get("v")
            if isinstance(raw, int):
                return cls.integer(raw)
            if isinstance(raw, str):
                return cls.integer(int(raw))
            raise ValueError(f"bad integer value {raw!r}")
        if kind == "real":
            raw = obj.get("v")
            if isinstance(raw, str):
                if raw == "nan":
                    return cls.real(float("nan"))
                if raw == "inf":
                    return cls.real(float("inf"))
                if raw == "-inf":
                    return cls.real(float("-inf"))
                raise ValueError(f"bad real value {raw!r}")
            return cls.real(float(raw))
        if kind == "text":
            return cls.text(obj["v"])
        if kind == "blob":
            return cls.blob(base64.b64decode(obj["v"]))
        raise ValueError(f"bad value tag {kind!r}")

    def display(self) -> str:
        if self.kind == NULL:
            return "NULL"
        if self.kind == INTEGER:
            return str(self.value)
        if self.kind == REAL:
            return repr(self.value)
        if self.kind == TEXT:
            return self.value
        return "x'" + self.value.hex() + "'"

    def __eq__(self, other) -> bool:
        return isinstance(other, TyValue) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TyValue({self.kind}, {self.display()!r})"


def make_null() -> TyValue:
    return TyValue.null()


def make_int(value: int) -> TyValue:
    return TyValue.integer(value)


def make_float(value: float) -> TyValue:
    return TyValue.real(value)


def make_text(value: str) -> TyValue:
    return TyValue.text(value)


def make_blob(value: bytes) -> TyValue:
    return TyValue.blob(value)


def encode_value(value: TyValue) -> dict:
    return value.encode()


def decode_value(obj: dict) -> TyValue:
    return TyValue.decode(obj)


ValueKey = Union[str, "tuple"]


def value_key(value: TyValue) -> str:
    """Stable string identity of one typed value."""
    return value.key()


def value_sort_key(value: TyValue):
    """Sort key with SQLite's NULL < numeric < text < blob ordering."""
    return value.sort_key()


def tuple_key(values) -> tuple[str, ...]:
    return tuple(v.key() for v in values)


def encode_tuple(values) -> list[dict]:
    return [v.encode() for v in values]


def decode_tuple(items) -> list[TyValue]:
    return [TyValue.decode(item) for item in items]
