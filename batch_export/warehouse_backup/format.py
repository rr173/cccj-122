"""On-disk format constants and the strictly-validated manifest model.

Format version 1 layout inside a backup directory::

    FORMAT                 # exactly b"warehouse-backup/1\\n"
    lock                   # flock(2) coordination file (never deleted)
    manifests/NNNNNN.json  # one manifest per backup id, published by rename
    chunks/hh/<64-hex>.chunk   # content-addressed chunk files
    tmp/                   # staging; nothing here is ever a backup

A manifest is a JSON object with *exactly* these keys::

    {
      "format": 1,
      "id": 3,                     # integer, must equal the file name number
      "parent": 2,                 # integer id or null for a full root
      "created_at": "ISO-8601 UTC",
      "image_size": 147456,        # bytes of the complete database image
      "image_sha256": "<64 hex>",  # digest of the complete image
      "chunk_size": 65536,         # must equal CHUNK_SIZE
      "chunk_count": 3,            # ceil(image_size / chunk_size)
      "chunks": [                  # only the chunks stored by THIS layer
        {"index": 1, "length": 65536, "sha256": "<64 hex>"},
        {"index": 2, "length": 16384, "sha256": "<64 hex>"}
      ]
    }

Chunk resolution rule (how a restore finds the bytes of chunk ``i``): walk the
manifest chain ``backup -> parent -> ... -> root`` and take the *first* layer
whose ``chunks`` list contains ``i``. A child lists every index whose bytes
differ from the parent image plus every index beyond the parent's
``chunk_count``; indices at or beyond the child's own ``chunk_count`` are never
consulted, so a longer parent tail can never leak into a shorter child.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from .errors import StoreCorruptionError

FORMAT_VERSION = 1
FORMAT_FILE_NAME = "FORMAT"
FORMAT_FILE_TEXT = b"warehouse-backup/1\n"
LOCK_FILE_NAME = "lock"
MANIFEST_DIR = "manifests"
CHUNK_DIR = "chunks"
STAGING_DIR = "tmp"

#: Fixed chunk size of the format. The last chunk of an image may be shorter.
CHUNK_SIZE = 65536

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_NAME_RE = re.compile(r"^([0-9]{6,})\.json$")
CHUNK_NAME_RE = re.compile(r"^([0-9a-f]{64})\.chunk$")
FANOUT_NAME_RE = re.compile(r"^[0-9a-f]{2}$")

MANIFEST_KEYS = {
    "format", "id", "parent", "created_at", "image_size", "image_sha256",
    "chunk_size", "chunk_count", "chunks",
}
CHUNK_KEYS = {"index", "length", "sha256"}


def manifest_file_name(backup_id: int) -> str:
    return f"{backup_id:06d}.json"


def chunk_count_for(image_size: int) -> int:
    """Number of chunks of a complete image; a zero-size image has none."""
    if image_size <= 0:
        return 0
    return (image_size + CHUNK_SIZE - 1) // CHUNK_SIZE


def expected_chunk_length(image_size: int, index: int) -> int:
    """Length every full chunk must have; the tail chunk may be shorter."""
    remaining = image_size - index * CHUNK_SIZE
    return min(CHUNK_SIZE, remaining)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_hex64(value) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


@dataclass(frozen=True)
class ChunkEntry:
    """One chunk stored by one manifest layer."""

    index: int
    length: int
    sha256: str

    def to_json(self) -> dict:
        return {"index": self.index, "length": self.length, "sha256": self.sha256}


@dataclass(frozen=True)
class Manifest:
    """One validated backup manifest (one layer of one backup)."""

    id: int
    parent: Optional[int]
    created_at: str
    image_size: int
    image_sha256: str
    chunk_count: int
    chunks: tuple  # tuple[ChunkEntry, ...] -- only this layer's own chunks

    def __post_init__(self):
        by_index = {}
        for entry in self.chunks:
            by_index[entry.index] = entry
        object.__setattr__(self, "_by_index", by_index)

    def entry_for(self, index: int) -> Optional[ChunkEntry]:
        return self._by_index.get(index)

    @property
    def is_root(self) -> bool:
        return self.parent is None

    def to_json(self) -> dict:
        return {
            "format": FORMAT_VERSION,
            "id": self.id,
            "parent": self.parent,
            "created_at": self.created_at,
            "image_size": self.image_size,
            "image_sha256": self.image_sha256,
            "chunk_size": CHUNK_SIZE,
            "chunk_count": self.chunk_count,
            "chunks": [entry.to_json() for entry in self.chunks],
        }

    def to_json_bytes(self) -> bytes:
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=2, sort_keys=False)
        return (text + "\n").encode("utf-8")

    @staticmethod
    def from_json(data, expected_id: int, where: str) -> "Manifest":
        """Strictly validate one parsed manifest. Any deviation is corruption."""
        bad = lambda msg: StoreCorruptionError(f"manifest {where}: {msg}")
        if not isinstance(data, dict):
            raise bad("top level is not a JSON object")
        unknown = set(data) - MANIFEST_KEYS
        if unknown:
            raise bad(f"unknown field(s) {sorted(unknown)}")
        missing = MANIFEST_KEYS - set(data)
        if missing:
            raise bad(f"missing field(s) {sorted(missing)}")

        if data["format"] != FORMAT_VERSION:
            raise bad(f"unsupported format version {data['format']!r}"
                      f" (this tool understands {FORMAT_VERSION})")

        if not _is_int(data["id"]) or data["id"] <= 0:
            raise bad(f"id must be a positive integer, got {data['id']!r}")
        if data["id"] != expected_id:
            raise bad(f"id {data['id']} does not match file name id {expected_id}")

        parent = data["parent"]
        if parent is not None:
            if not _is_int(parent) or parent <= 0:
                raise bad(f"parent must be null or a positive integer, got {parent!r}")
            if parent == data["id"]:
                raise bad(f"backup {data['id']} is its own parent")

        if not isinstance(data["created_at"], str) or not data["created_at"]:
            raise bad("created_at must be a non-empty string")

        if not _is_int(data["image_size"]) or data["image_size"] < 0:
            raise bad(f"image_size must be a non-negative integer, got"
                      f" {data['image_size']!r}")
        image_size = data["image_size"]

        if not _is_hex64(data["image_sha256"]):
            raise bad(f"image_sha256 must be 64 lowercase hex chars, got"
                      f" {data['image_sha256']!r}")

        if data["chunk_size"] != CHUNK_SIZE:
            raise bad(f"chunk_size must be {CHUNK_SIZE}, got {data['chunk_size']!r}")

        if not _is_int(data["chunk_count"]):
            raise bad(f"chunk_count must be an integer, got {data['chunk_count']!r}")
        want_count = chunk_count_for(image_size)
        if data["chunk_count"] != want_count:
            raise bad(f"chunk_count {data['chunk_count']} does not match"
                      f" image_size {image_size} (expected {want_count})")
        chunk_count = data["chunk_count"]

        raw_chunks = data["chunks"]
        if not isinstance(raw_chunks, list):
            raise bad("chunks must be a list")
        entries = []
        previous_index = -1
        for position, raw in enumerate(raw_chunks):
            if not isinstance(raw, dict):
                raise bad(f"chunks[{position}] is not an object")
            unknown = set(raw) - CHUNK_KEYS
            if unknown:
                raise bad(f"chunks[{position}] has unknown field(s) {sorted(unknown)}")
            missing = CHUNK_KEYS - set(raw)
            if missing:
                raise bad(f"chunks[{position}] missing field(s) {sorted(missing)}")
            index = raw["index"]
            if not _is_int(index) or not 0 <= index < chunk_count:
                raise bad(f"chunks[{position}].index {index!r} out of range"
                          f" 0..{chunk_count - 1}")
            if index <= previous_index:
                raise bad(f"chunk indices not strictly increasing at index {index}")
            previous_index = index
            length = raw["length"]
            want_length = expected_chunk_length(image_size, index)
            if not _is_int(length) or length != want_length:
                raise bad(f"chunks[{position}].length {length!r} does not match"
                          f" expected length {want_length} for chunk {index}")
            if not _is_hex64(raw["sha256"]):
                raise bad(f"chunks[{position}].sha256 must be 64 lowercase hex"
                          f" chars, got {raw['sha256']!r}")
            entries.append(ChunkEntry(index=index, length=length,
                                      sha256=raw["sha256"]))

        return Manifest(
            id=data["id"],
            parent=parent,
            created_at=data["created_at"],
            image_size=image_size,
            image_sha256=data["image_sha256"],
            chunk_count=chunk_count,
            chunks=tuple(entries),
        )
