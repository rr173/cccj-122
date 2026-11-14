"""Error types and outcome classification for the offline WAL change decoder.

Three result classes are distinguished throughout the tool:

* ``OK``            -- the whole WAL was parsed, every frame belongs to a
                       finished commit.
* ``INCOMPLETE``    -- every *committed* transaction was decoded, but the WAL
                       tail holds frames that never reached a commit (a partial
                       frame or uncommitted full frames).
* ``CORRUPT``       -- the WAL bytes violate the format or fail a checksum;
                       ``UNSUPPORTED`` covers inputs the tool deliberately
                       refuses (encrypted databases, virtual tables, schema
                       changes mid-WAL, ...).

Nothing that ends in INCOMPLETE/CORRUPT/UNSUPPORTED is ever reported as a
normal full result.
"""
from __future__ import annotations

from enum import Enum


class Outcome(str, Enum):
    OK = "ok"
    INCOMPLETE = "incomplete"
    UNSUPPORTED = "unsupported"
    CORRUPT = "corrupt"


class WalDecodeError(Exception):
    """Base class. ``offset`` is the byte position in the offending file and
    ``frame_index`` (0-based) names the frame when the problem is frame level."""

    def __init__(self, message: str, *, offset: int | None = None,
                 frame_index: int | None = None):
        super().__init__(message)
        self.message = message
        self.offset = offset
        self.frame_index = frame_index

    def location(self) -> str:
        parts = []
        if self.frame_index is not None:
            parts.append(f"frame #{self.frame_index} (0-based)")
        if self.offset is not None:
            parts.append(f"byte offset {self.offset}")
        return ", ".join(parts) if parts else "location unknown"

    def detailed(self) -> str:
        loc = self.location()
        return f"{self.message} [{loc}]" if loc != "location unknown" else self.message


class WalFormatError(WalDecodeError):
    """Header/frame structure or checksum violation -> CORRUPT."""


class WalUnsupportedError(WalDecodeError):
    """A legal SQLite feature/state this decoder refuses -> UNSUPPORTED."""


class InputValidationError(WalDecodeError):
    """Bad caller input (manifest mismatch, bad identity columns, ...).

    This is a usage error, never a decode result; nothing is produced."""
