"""Deterministic local stand-in for a real data warehouse.

No network, no real warehouse: rows are derived purely from
(version seed, row key), so a given query version always yields the
same data. Registering a new version simulates an upstream change;
re-registering an existing version with a different seed simulates a
silent data change underneath an in-flight export.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from .models import UnknownFieldError, UnknownVersionError

FIELDS = ("id", "country", "amount", "channel", "active")
_COUNTRIES = ("CN", "US", "DE", "JP", "BR", "IN")
_CHANNELS = ("web", "app", "api", "partner")

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
}


class LocalWarehouse:
    """A tiny deterministic queryable dataset with explicit versions."""

    def __init__(self, row_count: int = 1000):
        if row_count < 0:
            raise ValueError("row_count must be >= 0")
        self._row_count = row_count
        self._version_seeds: dict[str, str] = {}
        self._current_version: Optional[str] = None

    # -- version management -------------------------------------------------

    def register_version(self, version: str, seed: Optional[str] = None) -> None:
        self._version_seeds[version] = seed or hashlib.sha256(
            f"warehouse-seed:{version}".encode("utf-8")
        ).hexdigest()
        if self._current_version is None:
            self._current_version = version

    def set_current_version(self, version: str) -> None:
        if version not in self._version_seeds:
            raise UnknownVersionError(version)
        self._current_version = version

    @property
    def current_version(self) -> Optional[str]:
        return self._current_version

    def known_versions(self) -> list[str]:
        return sorted(self._version_seeds)

    # -- querying -------------------------------------------------------------

    def _row(self, version: str, key: int) -> dict:
        seed = self._version_seeds[version]
        h = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).digest()
        return {
            "id": key,
            "country": _COUNTRIES[h[0] % len(_COUNTRIES)],
            "amount": int.from_bytes(h[1:5], "big") % 1_000_000,  # cents, integer
            "channel": _CHANNELS[h[5] % len(_CHANNELS)],
            "active": bool(h[6] & 1),
        }

    def _matches(self, row: dict, filters: dict) -> bool:
        for field, cond in filters.items():
            if field not in FIELDS:
                raise UnknownFieldError(field)
            value = row[field]
            if isinstance(cond, dict):
                for op, operand in cond.items():
                    if op not in _OPS:
                        raise ValueError(f"unsupported filter operator: {op}")
                    if not _OPS[op](value, operand):
                        return False
            elif value != cond:
                return False
        return True

    def query(
        self,
        *,
        version: Optional[str] = None,
        projection: tuple[str, ...],
        filters: Optional[dict] = None,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> list[list[Any]]:
        """Run the query at an explicit version.

        Rows are returned sorted by the stable primary key ``id``; ``offset``
        and ``limit`` slice the sorted result so shards are deterministic.
        """
        version = version or self._current_version
        if version not in self._version_seeds:
            raise UnknownVersionError(version)
        for field in projection:
            if field not in FIELDS:
                raise UnknownFieldError(field)
        filters = filters or {}
        rows: list[list[Any]] = []
        skipped = 0
        for key in range(1, self._row_count + 1):
            row = self._row(version, key)
            if not self._matches(row, filters):
                continue
            if skipped < offset:
                skipped += 1
                continue
            rows.append([row[f] for f in projection])
            if limit is not None and len(rows) >= limit:
                break
        return rows
