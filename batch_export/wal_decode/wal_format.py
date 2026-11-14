"""Low-level SQLite WAL (Write-Ahead Log) binary parser.

Implements the on-disk format documented by SQLite, without ever handing the
WAL to SQLite itself:

* the 32-byte WAL header (magic, format version, page size, salts, header
  checksum) is validated field by field;
* frames are read lazily one at a time -- the whole WAL is never buffered;
* every frame's salt and cumulative rolling checksum are verified while
  reading, using the checksum byte order selected by the magic value (the
  stored header fields themselves are always big-endian);
* malformed input raises :class:`WalFormatError` carrying the byte offset
  and/or frame index instead of being treated as end-of-file.

This module knows nothing about tables or transactions -- it only exposes
frame boundaries. Transaction grouping and page materialisation live in
:mod:`wal_decode.decoder`.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import WalFormatError

WAL_MAGIC_LE = 0x377F0682  # checksums accumulate little-endian words
WAL_MAGIC_BE = 0x377F0683  # checksums accumulate big-endian words
WAL_FORMAT_VERSION = 3007000
WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24
MIN_PAGE_SIZE = 512
MAX_PAGE_SIZE = 65536
# The acceptance drills run 4096 and 65536; every power of two in the legal
# SQLite range is accepted.
LEGAL_PAGE_SIZES = {1 << k for k in range(9, 17)}


def rolling_checksum(data: bytes, s0: int, s1: int, *, big_endian: bool) -> tuple[int, int]:
    """One step of SQLite's WAL checksum over ``data`` (length multiple of 4).

    The accumulator pair (s0, s1) is carried across calls: the WAL header
    checksum seeds the first frame, each frame continues from the previous
    one. The word byte order depends on the WAL magic, not on the machine.
    """
    if len(data) % 4:
        raise ValueError("checksum input length must be a multiple of 4")
    prefix = ">" if big_endian else "<"
    words = struct.unpack(prefix + f"{len(data) // 4}I", data)
    mask = 0xFFFFFFFF
    for i in range(0, len(words), 2):
        s0 = (s0 + words[i] + s1) & mask
        s1 = (s1 + words[i + 1] + s0) & mask
    return s0, s1


@dataclass(frozen=True)
class WalHeader:
    magic: int
    format_version: int
    page_size: int
    checkpoint_seq: int
    salt1: int
    salt2: int
    checksum1: int
    checksum2: int

    @property
    def big_endian_checksums(self) -> bool:
        return self.magic == WAL_MAGIC_BE


@dataclass(frozen=True)
class Frame:
    """One fully read and checksum-verified WAL frame."""
    index: int            # 0-based frame ordinal within the file
    header_offset: int    # byte offset of the frame header in the WAL file
    page_no: int          # 1-based database page number
    db_size_after: int    # 0 unless this frame commits a transaction
    salt1: int
    salt2: int
    data: bytes           # exactly page_size bytes

    @property
    def is_commit(self) -> bool:
        return self.db_size_after != 0


@dataclass
class TailInfo:
    """Describes a non-empty, non-fatal tail (the result stays INCOMPLETE)."""
    offset: int                  # first byte that could not form a full frame
    full_frames_pending: int     # complete frames after the last commit frame
    partial_bytes: int           # leftover bytes that do not fill one frame
    frame_size: int              # 24 + page_size
    committed_frame_count: int   # frames belonging to finished transactions

    def describe(self) -> str:
        if self.partial_bytes:
            return (
                f"WAL truncated at byte offset {self.offset}: {self.partial_bytes} of "
                f"{self.frame_size} bytes for the next frame are present "
                f"(frame header is {FRAME_HEADER_SIZE} bytes); the partial frame is "
                "not zero-padded and is not treated as a commit"
            )
        if self.full_frames_pending:
            return (
                f"WAL ends at byte offset {self.offset} with {self.full_frames_pending} "
                "complete frame(s) that carry no commit marker; their pages are not "
                "materialised and produce no business changes"
            )
        return f"WAL ends at byte offset {self.offset}"


class WalFrameReader:
    """Lazily parse a WAL binary stream.

    Construction validates the 32-byte header. Iterate with :meth:`read_frame`
    until it returns ``None``; afterwards :attr:`tail` reports any uncommitted
    or partial tail. Structural violations raise :class:`WalFormatError`.
    """

    def __init__(self, stream):
        self._stream = stream
        raw = stream.read(WAL_HEADER_SIZE)
        if len(raw) == 0:
            self.header = None
            self.tail = None
            self.empty = True
            return
        self.empty = False
        if len(raw) < WAL_HEADER_SIZE:
            raise WalFormatError(
                f"WAL header truncated: {len(raw)} of {WAL_HEADER_SIZE} bytes present",
                offset=len(raw),
            )
        self.header = self._parse_header(raw)
        self.page_size = self.header.page_size
        self.frame_size = FRAME_HEADER_SIZE + self.page_size
        self._s0, self._s1 = self.header.checksum1, self.header.checksum2
        self._next_index = 0
        self._committed_frames = 0
        self._pending_frames = 0
        self.tail: TailInfo | None = None

    # -- header ---------------------------------------------------------------

    @staticmethod
    def _parse_header(raw: bytes) -> WalHeader:
        magic, version, page_size, ckpt_seq, salt1, salt2, c1, c2 = \
            struct.unpack(">IIIIIIII", raw)
        if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE):
            raise WalFormatError(
                f"bad WAL magic 0x{magic:08x}; expected 0x{WAL_MAGIC_LE:08x} "
                f"(little-endian checksums) or 0x{WAL_MAGIC_BE:08x} (big-endian)",
                offset=0,
            )
        if version != WAL_FORMAT_VERSION:
            raise WalFormatError(
                f"unsupported WAL format version {version}; expected {WAL_FORMAT_VERSION}",
                offset=4,
            )
        if page_size not in LEGAL_PAGE_SIZES:
            raise WalFormatError(
                f"illegal WAL page size {page_size}; expected a power of two between "
                f"{MIN_PAGE_SIZE} and {MAX_PAGE_SIZE}",
                offset=8,
            )
        big_endian = magic == WAL_MAGIC_BE
        h0, h1 = rolling_checksum(raw[:24], 0, 0, big_endian=big_endian)
        if (h0, h1) != (c1, c2):
            raise WalFormatError(
                f"WAL header checksum mismatch: stored (0x{c1:08x}, 0x{c2:08x}) "
                f"but recomputed (0x{h0:08x}, 0x{h1:08x}) over the first 24 header bytes",
                offset=24,
            )
        return WalHeader(magic, version, page_size, ckpt_seq, salt1, salt2, c1, c2)

    # -- frames ---------------------------------------------------------------

    def read_frame(self) -> Frame | None:
        """Return the next verified frame, or ``None`` at clean end of file."""
        if self.empty:
            return None
        offset = self._stream.tell()
        header_bytes = self._stream.read(FRAME_HEADER_SIZE)
        if len(header_bytes) == 0:
            # Clean EOF: still must surface complete frames that never committed.
            self._record_tail(offset)
            return None
        if len(header_bytes) < FRAME_HEADER_SIZE:
            # A cut frame header: corruption only if bytes are also missing for
            # the declared frame; classify as a truncated (incomplete) tail but
            # report the exact cut position.
            partial = header_bytes + self._stream.read(self.page_size)
            self.tail = TailInfo(
                offset=offset, full_frames_pending=self._pending_frames,
                partial_bytes=len(partial), frame_size=self.frame_size,
                committed_frame_count=self._committed_frames,
            )
            return None
        page_no, db_size_after, salt1, salt2, fc1, fc2 = \
            struct.unpack(">IIIIII", header_bytes)
        page_offset = offset + FRAME_HEADER_SIZE
        page_bytes = self._stream.read(self.page_size)
        if len(page_bytes) < self.page_size:
            self.tail = TailInfo(
                offset=offset, full_frames_pending=self._pending_frames,
                partial_bytes=FRAME_HEADER_SIZE + len(page_bytes),
                frame_size=self.frame_size,
                committed_frame_count=self._committed_frames,
            )
            return None

        index = self._next_index
        if salt1 != self.header.salt1 or salt2 != self.header.salt2:
            raise WalFormatError(
                f"frame salt (0x{salt1:08x}, 0x{salt2:08x}) differs from WAL header "
                f"salt (0x{self.header.salt1:08x}, 0x{self.header.salt2:08x}); the WAL "
                "was reset and this frame belongs to another checkpoint cycle",
                offset=offset + 8, frame_index=index,
            )
        if page_no == 0:
            raise WalFormatError(
                "frame declares database page number 0, which is never legal",
                offset=offset, frame_index=index,
            )
        big_endian = self.header.big_endian_checksums
        s0, s1 = rolling_checksum(header_bytes[:8], self._s0, self._s1,
                                  big_endian=big_endian)
        s0, s1 = rolling_checksum(page_bytes, s0, s1, big_endian=big_endian)
        if (s0, s1) != (fc1, fc2):
            raise WalFormatError(
                f"frame checksum mismatch: stored (0x{fc1:08x}, 0x{fc2:08x}) but "
                f"recomputed (0x{s0:08x}, 0x{s1:08x}) over the frame header prefix "
                "and page payload",
                offset=offset + 16, frame_index=index,
            )
        self._s0, self._s1 = s0, s1
        self._next_index += 1
        if db_size_after != 0:
            if page_no > db_size_after:
                raise WalFormatError(
                    f"commit frame writes page {page_no} but the transaction declares "
                    f"only {db_size_after} database page(s) after commit",
                    offset=offset, frame_index=index,
                )
            self._committed_frames = self._next_index
            self._pending_frames = 0
        else:
            self._pending_frames += 1
        return Frame(index, offset, page_no, db_size_after, salt1, salt2, page_bytes)

    def _record_tail(self, offset: int) -> None:
        if self._pending_frames:
            self.tail = TailInfo(
                offset=offset, full_frames_pending=self._pending_frames,
                partial_bytes=0, frame_size=self.frame_size,
                committed_frame_count=self._committed_frames,
            )

    @property
    def frame_count(self) -> int:
        return self._next_index
