"""On-disk registry layout, locking and strictly checked IO.

The registry is a self-contained directory that can be copied to another
machine and read offline. Every registered package is one immutable
directory named by the original ZIP's SHA-256; its members are stored
content-addressed under ``files/`` and all relational state lives in a single
SQLite catalog written *before* the directory is atomically renamed into
place. As with the backup tool, every file the tool itself opens must be a
real regular file -- a symlink is treated as corruption, never followed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import (
    InvocationError,
    PublishError,
    RegistryCorruptionError,
)

REGISTRY_FORMAT_TEXT = "taxonomy-package-registry 1\n"
_FORMAT_FILE = "FORMAT"
_LOCK_FILE = "lock"
_PACKAGES_DIR = "packages"
_FILES_DIR = "files"
_META_FILE = "meta.json"
_CATALOG_DB = "catalog.sqlite"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_STAGING_SUFFIX = ".staging-"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_ROOT_ENTRIES = frozenset({_FORMAT_FILE, _LOCK_FILE, _PACKAGES_DIR})
_PACKAGE_ENTRIES = frozenset({_META_FILE, _CATALOG_DB, _FILES_DIR})


def digest_dir_name(digest: str) -> str:
    if not _DIGEST_RE.match(digest):
        raise RegistryCorruptionError(f"{digest!r} is not a 64-char hex digest")
    return digest


class TaxonomyStore:
    """One taxonomy registry directory. Construction does not touch the disk."""

    def __init__(self, root):
        self.root = Path(os.path.realpath(str(root)))

    # -- paths -----------------------------------------------------------------

    @property
    def format_path(self) -> Path:
        return self.root / _FORMAT_FILE

    @property
    def lock_path(self) -> Path:
        return self.root / _LOCK_FILE

    @property
    def packages_dir(self) -> Path:
        return self.root / _PACKAGES_DIR

    def package_dir(self, digest: str) -> Path:
        return self.packages_dir / digest_dir_name(digest)

    def staging_dir(self, digest: str) -> Path:
        return self.packages_dir / (digest + _STAGING_SUFFIX + str(os.getpid()))

    def package_meta_path(self, digest: str) -> Path:
        return self.package_dir(digest) / _META_FILE

    def package_db_path(self, digest: str) -> Path:
        return self.package_dir(digest) / _CATALOG_DB

    def package_files_dir(self, digest: str) -> Path:
        return self.package_dir(digest) / _FILES_DIR

    def blob_path(self, digest: str, content_digest: str) -> Path:
        if not _DIGEST_RE.match(content_digest):
            raise RegistryCorruptionError(
                f"blob reference {content_digest!r} is not a 64-char hex digest")
        return self.package_files_dir(digest) / content_digest[:2] \
            / f"{content_digest}.member"

    # -- layout ----------------------------------------------------------------

    def exists(self) -> bool:
        return self.root.is_dir()

    def ensure_layout(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.packages_dir.mkdir(exist_ok=True)
            if not self.lock_path.exists():
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
                os.close(fd)
            if not self.format_path.exists():
                self._write_atomic(self.format_path,
                                   REGISTRY_FORMAT_TEXT.encode("utf-8"))
        except OSError as exc:
            raise PublishError(
                f"cannot initialise registry {self.root}: {exc}") from exc
        self.check_layout()

    def check_layout(self) -> list[str]:
        """Validate the fixed layout. Returns leftover staging directory names."""
        if not self.root.is_dir():
            raise InvocationError(f"registry {self.root} does not exist")
        leftovers: list[str] = []
        for entry in sorted(self.root.iterdir()):
            if entry.name not in _ROOT_ENTRIES:
                raise RegistryCorruptionError(
                    f"unexpected entry {entry.name!r} in registry root {self.root}")
        self._require_regular_file(self.format_path)
        if self.format_path.read_bytes() != REGISTRY_FORMAT_TEXT.encode("utf-8"):
            raise RegistryCorruptionError(
                f"FORMAT file of {self.root} has unexpected contents")
        self._require_real_dir(self.packages_dir)
        for entry in sorted(self.packages_dir.iterdir()):
            if _STAGING_SUFFIX in entry.name:
                leftovers.append(entry.name)
                continue
            if not _DIGEST_RE.match(entry.name) or not entry.is_dir() \
                    or entry.is_symlink():
                raise RegistryCorruptionError(
                    f"unexpected entry {entry.name!r} in {self.packages_dir}")
            self._check_package_layout(entry)
        return leftovers

    def _check_package_layout(self, package_dir: Path) -> None:
        for entry in sorted(package_dir.iterdir()):
            if entry.name not in _PACKAGE_ENTRIES:
                raise RegistryCorruptionError(
                    f"unexpected entry {entry.name!r} in package {package_dir}")
            if entry.is_symlink():
                raise RegistryCorruptionError(
                    f"{entry} is a symbolic link; substitution is refused")
        self._require_regular_file(package_dir / _META_FILE)
        self._require_regular_file(package_dir / _CATALOG_DB)
        self._require_real_dir(package_dir / _FILES_DIR)

    @staticmethod
    def _require_real_dir(path: Path) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise RegistryCorruptionError(f"missing directory {path}") from None
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise RegistryCorruptionError(f"{path} is not a real directory")

    @staticmethod
    def _require_regular_file(path: Path) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise RegistryCorruptionError(f"missing file {path}") from None
        if not stat.S_ISREG(st.st_mode):
            raise RegistryCorruptionError(
                f"{path} is not a regular file (symlink/special file refused)")

    # -- locking ----------------------------------------------------------------

    @contextmanager
    def locked(self, exclusive: bool) -> Iterator[None]:
        if not self.root.is_dir():
            raise InvocationError(f"registry {self.root} does not exist")
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- package publishing ------------------------------------------------------

    def package_exists(self, digest: str) -> bool:
        return self.package_dir(digest).is_dir()

    def list_package_dirs(self) -> list[str]:
        if not self.packages_dir.is_dir():
            return []
        return sorted(
            entry.name for entry in self.packages_dir.iterdir()
            if entry.is_dir() and _DIGEST_RE.match(entry.name))

    def create_staging(self, digest: str) -> Path:
        staging = self.staging_dir(digest)
        try:
            (staging / _FILES_DIR).mkdir(parents=True)
        except OSError as exc:
            raise PublishError(
                f"cannot create staging directory {staging}: {exc}") from exc
        return staging

    def remove_staging(self, staging: Path) -> None:
        import shutil
        try:
            shutil.rmtree(staging)
        except OSError:
            pass

    def publish_staging(self, staging: Path, digest: str) -> None:
        """Atomically rename a fully-built staging directory into place."""
        final = self.package_dir(digest)
        if final.exists():
            # Callers are expected to have detected this; never overwrite.
            raise PublishError(
                f"package directory {final} already exists; refusing to overwrite")
        try:
            os.replace(staging, final)
            dir_fd = os.open(self.packages_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            self.remove_staging(staging)
            raise PublishError(
                f"cannot publish package directory {final}: {exc}") from exc

    def write_blob(self, staging: Path, content_digest: str,
                   data: bytes) -> None:
        target = staging / _FILES_DIR / content_digest[:2] \
            / f"{content_digest}.member"
        if target.exists():
            return  # content-addressed: same bytes, one blob
        try:
            target.parent.mkdir(exist_ok=True)
            self._write_atomic(target, data)
        except PublishError:
            raise
        except OSError as exc:
            raise PublishError(f"cannot write blob {target}: {exc}") from exc

    def read_member_bytes(self, package_digest: str,
                          content_digest: str) -> bytes:
        path = self.blob_path(package_digest, content_digest)
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            raise RegistryCorruptionError(
                f"member blob {path} is missing") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RegistryCorruptionError(f"{path} is not a regular file")
            return os.read(fd, st.st_size)
        finally:
            os.close(fd)

    def iter_blob_paths(self, package_digest: str) -> list[Path]:
        files_dir = self.package_files_dir(package_digest)
        if not files_dir.is_dir():
            raise RegistryCorruptionError(f"missing files directory {files_dir}")
        found: list[Path] = []
        for fanout in sorted(files_dir.iterdir()):
            if fanout.is_symlink() or not fanout.is_dir() \
                    or not re.match(r"^[0-9a-f]{2}$", fanout.name):
                raise RegistryCorruptionError(
                    f"unexpected entry {fanout.name!r} in {files_dir}")
            for entry in sorted(fanout.iterdir()):
                if entry.is_symlink() or not entry.is_file() \
                        or not re.match(r"^[0-9a-f]{64}\.member$", entry.name):
                    raise RegistryCorruptionError(
                        f"unexpected entry {entry.name!r} in {fanout}")
                found.append(entry)
        return found

    def load_meta(self, digest: str) -> dict:
        path = self.package_meta_path(digest)
        self._require_regular_file(path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryCorruptionError(
                f"meta file {path} is unreadable or not valid JSON: {exc}") from exc

    def write_meta(self, staging: Path, meta: dict) -> None:
        text = json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True)
        self._write_atomic(staging / _META_FILE, text.encode("utf-8"))

    def atomic_replace_catalog(self, package_digest: str, mutate) -> None:
        """Copy the catalog to a staging file, let ``mutate`` edit it, rename.

        The published catalog is never opened writable in place, so a crash
        during a resolution publish cannot leave a half-written file and no
        WAL/journal side cars appear inside the immutable package directory.
        ``mutate`` receives the staging database path and must perform its
        own transaction; any exception removes the staging file and leaves
        the published catalog untouched.
        """
        import shutil
        published = self.package_db_path(package_digest)
        staging = published.with_name(
            f"{_CATALOG_DB}.staging-{os.getpid()}")
        try:
            shutil.copyfile(published, staging)
            mutate(str(staging))
            os.replace(staging, published)
            dir_fd = os.open(published.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
            for sidecar in (staging.with_suffix(".sqlite-wal"),
                            staging.with_suffix(".sqlite-shm"),
                            published.with_name(
                                f"{_CATALOG_DB}.staging-{os.getpid()}-wal"),
                            published.with_name(
                                f"{_CATALOG_DB}.staging-{os.getpid()}-shm")):
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def hash_file(path: Path) -> str:
        hasher = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        try:
            for block in iter(lambda: os.read(fd, 1 << 20), b""):
                hasher.update(block)
        finally:
            os.close(fd)
        return hasher.hexdigest()

    @staticmethod
    def _write_atomic(final: Path, data: bytes) -> None:
        staging = final.with_name(final.name + ".tmp")
        try:
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(staging, final)
        except OSError as exc:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
            raise PublishError(f"cannot write {final}: {exc}") from exc


def check_digest_value(digest: str) -> None:
    if not _DIGEST_RE.match(digest or ""):
        raise InvocationError(f"{digest!r} is not a registered package digest")
