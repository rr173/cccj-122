"""Filesystem layout, locking and strictly-checked IO for one backup directory.

Everything the tool reads or writes lives directly under the backup directory
in ``FORMAT``, ``lock``, ``manifests/``, ``chunks/`` or ``tmp/``. Backup ids
are integers and chunk names are validated 64-char hex digests, so no manifest
content can ever be turned into a path that escapes the directory. Every file
the tool opens is required to be a regular file -- a symlink anywhere in the
layout is treated as store corruption, never followed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .errors import InvocationError, StoreCorruptionError, TargetWriteError
from .format import (
    CHUNK_DIR,
    CHUNK_NAME_RE,
    FANOUT_NAME_RE,
    FORMAT_FILE_NAME,
    FORMAT_FILE_TEXT,
    LOCK_FILE_NAME,
    MANIFEST_DIR,
    MANIFEST_NAME_RE,
    STAGING_DIR,
    Manifest,
    manifest_file_name,
)
from . import format as fmt

_ROOT_ENTRIES = {FORMAT_FILE_NAME, LOCK_FILE_NAME, MANIFEST_DIR, CHUNK_DIR,
                 STAGING_DIR}
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class BackupStore:
    """One backup directory. Constructing does not touch the filesystem."""

    def __init__(self, root):
        self.root = Path(os.path.realpath(str(root)))

    # -- paths (all derived from validated integers / hex digests only) ------

    @property
    def format_path(self) -> Path:
        return self.root / FORMAT_FILE_NAME

    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_FILE_NAME

    @property
    def manifest_dir(self) -> Path:
        return self.root / MANIFEST_DIR

    @property
    def chunk_dir(self) -> Path:
        return self.root / CHUNK_DIR

    @property
    def staging_dir(self) -> Path:
        return self.root / STAGING_DIR

    def manifest_path(self, backup_id: int) -> Path:
        return self.manifest_dir / manifest_file_name(backup_id)

    def chunk_path(self, digest: str) -> Path:
        if not fmt._is_hex64(digest):
            raise StoreCorruptionError(
                f"chunk reference {digest!r} is not a 64-char hex digest")
        return self.chunk_dir / digest[:2] / f"{digest}.chunk"

    def staging_path(self, name: str) -> Path:
        return self.staging_dir / name

    # -- layout ----------------------------------------------------------------

    def exists(self) -> bool:
        return self.root.is_dir()

    def ensure_layout(self) -> None:
        """Create the layout (idempotent). Called by ``create`` under the lock."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for directory in (self.manifest_dir, self.chunk_dir, self.staging_dir):
                directory.mkdir(exist_ok=True)
            if not self.format_path.exists():
                self._write_file_atomically(self.format_path, FORMAT_FILE_TEXT)
        except OSError as exc:
            raise TargetWriteError(
                f"cannot initialise backup directory {self.root}: {exc}") from exc
        self.check_layout()

    def check_layout(self) -> None:
        """Verify the fixed layout; anything unexpected is corruption."""
        if not self.root.is_dir():
            raise InvocationError(f"backup directory {self.root} does not exist")
        for entry in sorted(self.root.iterdir()):
            if entry.name not in _ROOT_ENTRIES:
                raise StoreCorruptionError(
                    f"unexpected entry {entry.name!r} in backup directory"
                    f" {self.root}")
        self._require_regular_file(self.format_path)
        try:
            actual = self.format_path.read_bytes()
        except OSError as exc:
            raise StoreCorruptionError(f"cannot read FORMAT file: {exc}") from exc
        if actual != FORMAT_FILE_TEXT:
            raise StoreCorruptionError(
                f"FORMAT file of {self.root} says {actual!r}; this tool"
                f" understands {FORMAT_FILE_TEXT!r}")
        for directory in (self.manifest_dir, self.chunk_dir, self.staging_dir):
            self._require_real_dir(directory)

    @staticmethod
    def _require_real_dir(path: Path) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise StoreCorruptionError(f"missing directory {path}") from None
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise StoreCorruptionError(f"{path} is not a real directory")

    @staticmethod
    def _require_regular_file(path: Path) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise StoreCorruptionError(f"missing file {path}") from None
        if not stat.S_ISREG(st.st_mode):
            raise StoreCorruptionError(
                f"{path} is not a regular file (symlink or special file"
                f" substitution is refused)")

    # -- inter-process coordination ---------------------------------------------

    @contextmanager
    def locked(self, exclusive: bool) -> Iterator[None]:
        """Hold the store-wide flock. Restores/checks share it; creates and
        prune-applies take it exclusively, so a prune can never delete a chunk
        while another process is reading it."""
        if not self.root.is_dir():
            raise InvocationError(f"backup directory {self.root} does not exist")
        try:
            fd = os.open(self.lock_path,
                         os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o644)
        except OSError as exc:
            raise InvocationError(
                f"cannot open lock file in {self.root}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- manifests ---------------------------------------------------------------

    def manifest_ids(self) -> list[int]:
        ids = []
        for entry in sorted(self.manifest_dir.iterdir()):
            if not entry.is_file() or entry.is_symlink():
                raise StoreCorruptionError(
                    f"{entry} is not a regular manifest file")
            match = MANIFEST_NAME_RE.match(entry.name)
            if not match:
                raise StoreCorruptionError(
                    f"unexpected file {entry.name!r} in {self.manifest_dir}")
            ids.append(int(match.group(1)))
        return sorted(ids)

    def manifest_exists(self, backup_id: int) -> bool:
        return self.manifest_path(backup_id).is_file()

    def load_manifest(self, backup_id: int) -> Manifest:
        path = self.manifest_path(backup_id)
        self._require_regular_file(path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise StoreCorruptionError(f"cannot read {path}: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreCorruptionError(f"manifest {path} is not valid JSON: {exc}")
        return Manifest.from_json(data, expected_id=backup_id, where=str(path))

    def load_all_manifests(self) -> list[Manifest]:
        return [self.load_manifest(bid) for bid in self.manifest_ids()]

    def allocate_id(self) -> int:
        """Next free backup id. Caller must hold the exclusive lock."""
        ids = self.manifest_ids()
        return ids[-1] + 1 if ids else 1

    def publish_manifest(self, manifest: Manifest) -> None:
        """Atomically publish a fully-written manifest (staging -> rename)."""
        staging = self.staging_path(f"manifest-{os.getpid()}.json")
        self._write_file_atomically(self.manifest_path(manifest.id),
                                    manifest.to_json_bytes(), staging=staging)

    def remove_manifest(self, backup_id: int) -> None:
        try:
            self.manifest_path(backup_id).unlink()
        except OSError as exc:
            raise TargetWriteError(f"cannot remove manifest {backup_id:06d}: {exc}")

    # -- chunks ------------------------------------------------------------------

    def store_chunk(self, digest: str, data: bytes) -> bool:
        """Store one chunk content-addressed. Returns True if it was already
        present (shared chunk reused, not written again). An existing file
        whose content does not match its name is corruption, never repaired."""
        final = self.chunk_path(digest)
        if final.exists() or final.is_symlink():
            self._require_regular_file(final)
            existing = self._hash_file(final)
            if existing != digest:
                raise StoreCorruptionError(
                    f"chunk file {final} content does not match its name")
            return True
        fanout = final.parent
        try:
            fanout.mkdir(exist_ok=True)
        except OSError as exc:
            raise TargetWriteError(f"cannot create {fanout}: {exc}") from exc
        self._require_real_dir(fanout)
        staging = self.staging_path(f"chunk-{os.getpid()}-{digest[:12]}")
        self._write_file_atomically(final, data, staging=staging)
        return False

    def read_chunk(self, digest: str, expected_length: int) -> bytes:
        """Read and fully verify one chunk (regular file, length, digest)."""
        path = self.chunk_path(digest)
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            raise StoreCorruptionError(f"chunk file {path} is missing") from None
        except OSError as exc:
            raise StoreCorruptionError(f"cannot open chunk {path}: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StoreCorruptionError(f"{path} is not a regular file")
            if st.st_size != expected_length:
                raise StoreCorruptionError(
                    f"chunk {path} has {st.st_size} bytes, manifest says"
                    f" {expected_length}")
            hasher = hashlib.sha256()
            parts = []
            while True:
                block = os.read(fd, 1 << 20)
                if not block:
                    break
                parts.append(block)
                hasher.update(block)
        finally:
            os.close(fd)
        if hasher.hexdigest() != digest:
            raise StoreCorruptionError(
                f"chunk {path} content digest does not match its name")
        return b"".join(parts)

    def iter_chunks(self) -> list[tuple[str, Path, int]]:
        """All chunk files on disk as (digest, path, size); strict layout."""
        found = []
        if not self.chunk_dir.is_dir():
            return found
        for fanout in sorted(self.chunk_dir.iterdir()):
            if not FANOUT_NAME_RE.match(fanout.name) or not fanout.is_dir() \
                    or fanout.is_symlink():
                raise StoreCorruptionError(
                    f"unexpected entry {fanout.name!r} in {self.chunk_dir}")
            for entry in sorted(fanout.iterdir()):
                match = CHUNK_NAME_RE.match(entry.name)
                if not match or not entry.is_file() or entry.is_symlink():
                    raise StoreCorruptionError(
                        f"unexpected entry {entry.name!r} in {fanout}")
                found.append((match.group(1), entry, entry.stat().st_size))
        return found

    def remove_chunk(self, digest: str) -> None:
        path = self.chunk_path(digest)
        try:
            path.unlink()
        except OSError as exc:
            raise TargetWriteError(f"cannot remove chunk {path}: {exc}") from exc
        try:
            path.parent.rmdir()  # only succeeds when the fan-out dir is empty
        except OSError:
            pass

    # -- staging -------------------------------------------------------------------

    def iter_staging(self) -> list[Path]:
        """Leftover staging files (failed creations). Never restorable data."""
        if not self.staging_dir.is_dir():
            return []
        found = []
        for entry in sorted(self.staging_dir.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                raise StoreCorruptionError(
                    f"unexpected directory {entry.name!r} in {self.staging_dir}")
            found.append(entry)
        return found

    def remove_staging(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError as exc:
            raise TargetWriteError(f"cannot remove staging file {path}: {exc}")

    # -- helpers ---------------------------------------------------------------------

    @staticmethod
    def _hash_file(path: Path) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(block)
        return hasher.hexdigest()

    def _write_file_atomically(self, final: Path, data: bytes,
                               staging: Optional[Path] = None) -> None:
        staging = staging or final.with_name(final.name + ".tmp")
        try:
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(staging, final)
            dir_fd = os.open(final.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
            raise TargetWriteError(f"cannot write {final}: {exc}") from exc
