"""Unit tests for the low-level WAL parser, checksums and input validation."""
from __future__ import annotations

import os
import shutil
import sqlite3
import struct
import tempfile
import unittest

from wal_decode.errors import InputValidationError, WalFormatError
from wal_decode.wal_format import (
    FRAME_HEADER_SIZE,
    WAL_HEADER_SIZE,
    WAL_MAGIC_BE,
    WAL_MAGIC_LE,
    WalFrameReader,
    rolling_checksum,
)


class ChecksumTests(unittest.TestCase):
    def test_known_vectors_both_orders(self):
        # Hand-derived step by step from the documented recurrence:
        #   s0 = s0 + words[i]   + s1
        #   s1 = s1 + words[i+1] + s0   (updated s0)
        data = bytes(range(8))
        le0, le1 = rolling_checksum(data, 0, 0, big_endian=False)
        be0, be1 = rolling_checksum(data, 0, 0, big_endian=True)
        mask = 0xFFFFFFFF
        # little-endian words: 0x03020100, 0x07060504
        w0, w1 = 0x03020100, 0x07060504
        s0 = (0 + w0 + 0) & mask
        s1 = (0 + w1 + s0) & mask
        self.assertEqual((le0, le1), (s0, s1))
        # big-endian words: 0x00010203, 0x04050607
        w0, w1 = 0x00010203, 0x04050607
        s0 = w0
        s1 = (w1 + s0) & mask
        self.assertEqual((be0, be1), (s0, s1))
        self.assertNotEqual((le0, le1), (be0, be1))

    def test_accumulator_is_carried_across_calls(self):
        data = bytes(range(16))
        one = rolling_checksum(data, 7, 9, big_endian=False)
        first = rolling_checksum(data[:8], 7, 9, big_endian=False)
        second = rolling_checksum(data[8:], first[0], first[1], big_endian=False)
        self.assertEqual(one, second)

    def test_requires_word_multiple(self):
        with self.assertRaises(ValueError):
            rolling_checksum(b"abc", 0, 0, big_endian=False)


class WalBuilder:
    """Tiny helper producing real SQLite WAL files for parser tests."""

    def __init__(self, page_size=4096):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "w.sqlite")
        self.page_size = page_size
        conn = sqlite3.connect(self.db)
        conn.execute(f"PRAGMA page_size={page_size}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'seed')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.base = os.path.join(self.tmp, "base.sqlite")
        shutil.copyfile(self.db, self.base)
        self.conn = conn

    def commit_more(self, *sqls):
        for sql in sqls:
            self.conn.execute(sql)
        self.conn.commit()

    def wal_bytes(self):
        with open(self.db + "-wal", "rb") as fh:
            return fh.read()

    def write_wal(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def close(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class FrameReaderTests(unittest.TestCase):
    def test_parses_real_wal_and_commit_markers(self):
        b = WalBuilder()
        b.commit_more("INSERT INTO t VALUES (2,'a')")
        b.commit_more("INSERT INTO t VALUES (3,'b')")
        path = b.write_wal("x.wal", b.wal_bytes())
        with open(path, "rb") as fh:
            reader = WalFrameReader(fh)
            self.assertFalse(reader.empty)
            self.assertEqual(reader.header.page_size, 4096)
            self.assertIn(reader.header.magic, (WAL_MAGIC_LE, WAL_MAGIC_BE))
            frames = []
            while True:
                frame = reader.read_frame()
                if frame is None:
                    break
                frames.append(frame)
            self.assertGreaterEqual(len(frames), 2)
            self.assertTrue(frames[-1].is_commit)
            self.assertEqual(reader.tail, None)
        b.close()

    def test_empty_wal(self):
        b = WalBuilder()
        path = b.write_wal("empty.wal", b"")
        with open(path, "rb") as fh:
            reader = WalFrameReader(fh)
            self.assertTrue(reader.empty)
            self.assertIsNone(reader.read_frame())
        b.close()

    def test_truncated_header_is_corruption(self):
        b = WalBuilder()
        path = b.write_wal("h.wal", b"\x00" * 10)
        with open(path, "rb") as fh:
            with self.assertRaises(WalFormatError) as ctx:
                WalFrameReader(fh)
            self.assertEqual(ctx.exception.offset, 10)
        b.close()

    def test_bad_magic_reports_offset_zero(self):
        b = WalBuilder()
        b.commit_more("INSERT INTO t VALUES (2,'a')")
        raw = bytearray(b.wal_bytes())
        raw[0] ^= 0xFF
        path = b.write_wal("magic.wal", bytes(raw))
        with open(path, "rb") as fh:
            with self.assertRaises(WalFormatError) as ctx:
                WalFrameReader(fh)
            self.assertEqual(ctx.exception.offset, 0)
        b.close()

    def test_header_checksum_mutation_detected(self):
        b = WalBuilder()
        b.commit_more("INSERT INTO t VALUES (2,'a')")
        raw = bytearray(b.wal_bytes())
        raw[24] ^= 0x01  # first checksum byte
        path = b.write_wal("hc.wal", bytes(raw))
        with open(path, "rb") as fh:
            with self.assertRaises(WalFormatError) as ctx:
                WalFrameReader(fh)
            self.assertEqual(ctx.exception.offset, 24)
        b.close()

    def test_page_number_zero_rejected(self):
        b = WalBuilder()
        b.commit_more("INSERT INTO t VALUES (2,'a')")
        raw = bytearray(b.wal_bytes())
        # First frame page number sits at WAL header + frame header start.
        struct.pack_into(">I", raw, WAL_HEADER_SIZE, 0)
        path = b.write_wal("pg0.wal", bytes(raw))
        with open(path, "rb") as fh:
            reader = WalFrameReader(fh)
            with self.assertRaises(WalFormatError) as ctx:
                reader.read_frame()
            self.assertEqual(ctx.exception.frame_index, 0)
            self.assertEqual(ctx.exception.offset, WAL_HEADER_SIZE)
        b.close()

    def test_salt_change_rejected(self):
        b = WalBuilder()
        b.commit_more("INSERT INTO t VALUES (2,'a')")
        raw = bytearray(b.wal_bytes())
        # Frame salt1 at header + frame header + 8
        off = WAL_HEADER_SIZE + 8
        raw[off] ^= 0x0A
        path = b.write_wal("salt.wal", bytes(raw))
        with open(path, "rb") as fh:
            reader = WalFrameReader(fh)
            with self.assertRaises(WalFormatError) as ctx:
                reader.read_frame()
            self.assertEqual(ctx.exception.frame_index, 0)
            self.assertEqual(ctx.exception.offset, off)
        b.close()

    def test_frame_checksum_mutation_detected(self):
        b = WalBuilder()
        b.commit_more("INSERT INTO t VALUES (2,'a')")
        raw = bytearray(b.wal_bytes())
        off = WAL_HEADER_SIZE + 16
        raw[off] ^= 0x01
        path = b.write_wal("fc.wal", bytes(raw))
        with open(path, "rb") as fh:
            reader = WalFrameReader(fh)
            with self.assertRaises(WalFormatError) as ctx:
                reader.read_frame()
            self.assertEqual(ctx.exception.frame_index, 0)
            self.assertEqual(ctx.exception.offset, off)
        b.close()

    def test_truncated_mid_frame_is_incomplete_not_corrupt(self):
        b = WalBuilder()
        b.commit_more("INSERT INTO t VALUES (2,'a')")
        b.commit_more("INSERT INTO t VALUES (3,'b')")
        full = b.wal_bytes()
        frame_size = FRAME_HEADER_SIZE + 4096
        cut = WAL_HEADER_SIZE + frame_size + 77
        path = b.write_wal("cut.wal", full[:cut])
        with open(path, "rb") as fh:
            reader = WalFrameReader(fh)
            count = 0
            while reader.read_frame() is not None:
                count += 1
            self.assertIsNotNone(reader.tail)
            # 77 bytes remain after the previous full frame: 24-byte header
            # plus 53 bytes of the 4096-byte page payload.
            self.assertEqual(reader.tail.partial_bytes, 77)
            self.assertEqual(count, reader.tail.committed_frame_count)
        b.close()

    def test_full_frame_without_commit_marker_pending(self):
        # Simulate a crash after frames were written but before the commit
        # marker: rebuild a syntactically valid last frame whose
        # db_size_after=0 with a correctly recomputed checksum. The checksum
        # is cumulative, so it must continue from the preceding frame state.
        b = WalBuilder()
        b.commit_more("INSERT INTO t VALUES (2,'a')")
        b.commit_more("INSERT INTO t VALUES (3,'b')")
        raw = bytearray(b.wal_bytes())
        frame_size = FRAME_HEADER_SIZE + 4096
        big_endian = raw[:4] == struct.pack(">I", WAL_MAGIC_BE)
        # Replay the checksum chain to reach the state just before the last
        # frame: header, then every complete earlier frame.
        s0, s1 = struct.unpack(">II", bytes(raw[24:32]))
        frame_offsets = list(range(WAL_HEADER_SIZE, len(raw), frame_size))
        last = frame_offsets[-1]
        for off in frame_offsets[:-1]:
            s0, s1 = rolling_checksum(bytes(raw[off:off + 8]), s0, s1,
                                      big_endian=big_endian)
            s0, s1 = rolling_checksum(
                bytes(raw[off + FRAME_HEADER_SIZE:off + frame_size]),
                s0, s1, big_endian=big_endian,
            )
        struct.pack_into(">I", raw, last + 4, 0)  # clear commit marker
        s0, s1 = rolling_checksum(bytes(raw[last:last + 8]), s0, s1,
                                  big_endian=big_endian)
        s0, s1 = rolling_checksum(
            bytes(raw[last + FRAME_HEADER_SIZE:last + frame_size]),
            s0, s1, big_endian=big_endian,
        )
        struct.pack_into(">II", raw, last + 16, s0, s1)
        path = b.write_wal("pending.wal", bytes(raw))
        with open(path, "rb") as fh:
            reader = WalFrameReader(fh)
            frames = []
            while True:
                frame = reader.read_frame()
                if frame is None:
                    break
                frames.append(frame)
            self.assertIsNotNone(reader.tail)
            self.assertEqual(reader.tail.full_frames_pending, 1)
            self.assertFalse(frames[-1].is_commit)
        b.close()


class IdentifierTests(unittest.TestCase):
    def test_quote_identifier_rejects_injection(self):
        from wal_decode.manifest import quote_identifier
        self.assertEqual(quote_identifier("facts"), '"facts"')
        for bad in ('x"; DROP TABLE t; --', "x-y", "1col", "", "a b"):
            with self.assertRaises(InputValidationError):
                quote_identifier(bad)


if __name__ == "__main__":
    unittest.main()
