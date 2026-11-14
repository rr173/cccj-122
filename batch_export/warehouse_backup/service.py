"""Create / check / restore / prune logic for the warehouse backup tool.

Consistency model:

* The live database is copied with SQLite's online backup API
  (``sqlite3.Connection.backup``), the supported way to take a consistent
  image of a database that other connections keep writing to -- possibly in
  WAL mode. The caller never has to stop writers or run a checkpoint, the
  image always corresponds to one fully committed state, and the source
  database is never modified (no checkpoint, no extra tables).
* The image is cut into fixed 65536-byte chunks (last one may be short) and
  stored content-addressed; a child manifest lists only the chunks that
  changed against its parent and inherits the rest along the parent chain.
* Restore resolves the chain layer by layer, verifies every chunk and the
  whole image against the manifest, runs SQLite's own integrity check, and
  only then atomically replaces the target. A failed restore never touches a
  pre-existing target.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .errors import InvocationError, StoreCorruptionError, TargetWriteError
from .format import CHUNK_SIZE, ChunkEntry, Manifest
from .store import BackupStore

#: How long the drill pause gate waits for the "go" signal before giving up.
PAUSE_GATE_TIMEOUT_SECONDS = 120.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# chain resolution
# ---------------------------------------------------------------------------

def load_chain(store: BackupStore, backup_id: int) -> list[Manifest]:
    """Validated manifest chain [backup, parent, ..., root].

    Missing parents and cycles are store corruption; the restore never
    silently falls back to an older or different backup.
    """
    chain: list[Manifest] = []
    seen: set[int] = set()
    current = backup_id
    while True:
        if current in seen:
            raise StoreCorruptionError(
                f"parent chain of backup {backup_id:06d} contains a cycle"
                f" at backup {current:06d}")
        seen.add(current)
        if not store.manifest_exists(current):
            raise StoreCorruptionError(
                f"parent chain of backup {backup_id:06d} needs manifest"
                f" {current:06d}, which is missing")
        manifest = store.load_manifest(current)
        chain.append(manifest)
        if manifest.parent is None:
            return chain
        current = manifest.parent


def resolve_chunks(chain: list[Manifest]) -> dict[int, tuple[int, ChunkEntry]]:
    """For each chunk index of the chain head: the layer providing it.

    The providing layer is the nearest manifest on the path to the root that
    lists the index -- see the format docstring for the full rule.
    """
    target = chain[0]
    resolution: dict[int, tuple[int, ChunkEntry]] = {}
    for index in range(target.chunk_count):
        for manifest in chain:
            entry = manifest.entry_for(index)
            if entry is not None:
                resolution[index] = (manifest.id, entry)
                break
        else:
            raise StoreCorruptionError(
                f"no layer in the chain of backup {target.id:06d} provides"
                f" chunk {index}; the chain is incomplete")
    return resolution


def _validate_graph(manifests: list[Manifest]) -> dict[int, list[Manifest]]:
    """Validate every parent link of every manifest; return chains by id."""
    by_id = {m.id: m for m in manifests}
    chains: dict[int, list[Manifest]] = {}
    for manifest in manifests:
        chain: list[Manifest] = []
        seen: set[int] = set()
        current = manifest
        while True:
            if current.id in seen:
                raise StoreCorruptionError(
                    f"parent chain of backup {manifest.id:06d} contains a"
                    f" cycle at backup {current.id:06d}")
            seen.add(current.id)
            chain.append(current)
            if current.parent is None:
                break
            parent = by_id.get(current.parent)
            if parent is None:
                raise StoreCorruptionError(
                    f"backup {current.id:06d} names parent"
                    f" {current.parent:06d}, which is missing")
            current = parent
        chains[manifest.id] = chain
    return chains


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateReport:
    backup_id: int
    parent: Optional[int]
    image_size: int
    image_sha256: str
    chunk_count: int
    layer_chunks: int        # chunks listed by the new manifest
    new_chunk_files: int     # chunk files actually written
    reused_chunk_files: int  # listed chunks already present (shared)


def _snapshot_database(source_path: str, image_path: Path) -> None:
    """Consistent standalone image of a live database via the backup API.

    The source may be in WAL mode and may be written by other connections
    while this runs; the image always matches one fully committed state.
    """
    if not os.path.isfile(source_path):
        raise InvocationError(f"source database {source_path} does not exist")
    try:
        source = sqlite3.connect(source_path)
    except sqlite3.Error as exc:
        raise InvocationError(f"cannot open source database {source_path}:"
                              f" {exc}") from exc
    try:
        target = sqlite3.connect(str(image_path), isolation_level=None)
        try:
            source.backup(target)
            # Make the image a plain rollback-journal database so it opens
            # read-only anywhere without WAL side files.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise InvocationError(f"cannot snapshot {source_path}: {exc}") from exc
    finally:
        source.close()


def create_backup(store: BackupStore, source_db: str,
                  parent: Optional[int] = None) -> CreateReport:
    """Back up the live database into the store as a new numbered backup.

    The whole operation holds the store lock exclusively, so a concurrent
    prune can never remove a chunk this creation is publishing or reusing.
    The temporary snapshot image is deleted before returning and never plays
    a role in any later restore.
    """
    if not store.exists():
        try:
            store.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TargetWriteError(
                f"cannot create backup directory {store.root}: {exc}") from exc
    with store.locked(exclusive=True):
        store.ensure_layout()
        parent_chain: Optional[list[Manifest]] = None
        if parent is not None:
            if not store.manifest_exists(parent):
                raise InvocationError(
                    f"parent backup {parent:06d} does not exist in"
                    f" {store.root}")
            parent_chain = load_chain(store, parent)
        parent_resolution = resolve_chunks(parent_chain) if parent_chain else {}

        image = store.staging_path(f"image-{os.getpid()}")
        try:
            _snapshot_database(source_db, image)

            image_hasher = hashlib.sha256()
            layer: list[ChunkEntry] = []
            new_files = reused_files = 0
            image_size = 0
            index = 0
            with open(image, "rb") as handle:
                while True:
                    block = handle.read(CHUNK_SIZE)
                    if not block:
                        break
                    image_hasher.update(block)
                    image_size += len(block)
                    digest = hashlib.sha256(block).hexdigest()
                    inherited = parent_resolution.get(index)
                    if inherited is None or inherited[1].sha256 != digest:
                        if store.store_chunk(digest, block):
                            reused_files += 1
                        else:
                            new_files += 1
                        layer.append(ChunkEntry(index=index, length=len(block),
                                                sha256=digest))
                    index += 1

            manifest = Manifest(
                id=store.allocate_id(),
                parent=parent,
                created_at=_utcnow(),
                image_size=image_size,
                image_sha256=image_hasher.hexdigest(),
                chunk_count=index,
                chunks=tuple(layer),
            )
            store.publish_manifest(manifest)
        finally:
            # The creation-time image is staging only; restores never see it.
            image.unlink(missing_ok=True)

        return CreateReport(
            backup_id=manifest.id,
            parent=manifest.parent,
            image_size=manifest.image_size,
            image_sha256=manifest.image_sha256,
            chunk_count=manifest.chunk_count,
            layer_chunks=len(layer),
            new_chunk_files=new_files,
            reused_chunk_files=reused_files,
        )


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BackupCheck:
    backup_id: int
    ok: bool
    detail: str
    layers: int = 0
    chunks_verified: int = 0
    bytes_verified: int = 0


@dataclass(frozen=True)
class CheckReport:
    checks: tuple          # tuple[BackupCheck, ...]
    unreferenced_chunks: int

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def check_backups(store: BackupStore,
                  backup_id: Optional[int] = None) -> CheckReport:
    """Verify manifests, chains and every needed chunk of one or all backups.

    Each requested backup's chunks are re-hashed and the assembled byte
    stream is compared against the manifest's whole-image digest.
    """
    with store.locked(exclusive=False):
        store.check_layout()
        manifests = store.load_all_manifests()
        chains = _validate_graph(manifests)
        by_id = {m.id: m for m in manifests}

        if backup_id is not None:
            if backup_id not in by_id:
                raise InvocationError(
                    f"backup {backup_id:06d} does not exist in {store.root}")
            targets = [backup_id]
        else:
            targets = sorted(by_id)

        checks: list[BackupCheck] = []
        for target_id in targets:
            chain = chains[target_id]
            resolution = resolve_chunks(chain)
            target = chain[0]
            image_hasher = hashlib.sha256()
            total = 0
            for index in range(target.chunk_count):
                _layer, entry = resolution[index]
                data = store.read_chunk(entry.sha256, entry.length)
                image_hasher.update(data)
                total += len(data)
            if total != target.image_size:
                raise StoreCorruptionError(
                    f"backup {target_id:06d}: assembled {total} bytes, manifest"
                    f" says {target.image_size}")
            if image_hasher.hexdigest() != target.image_sha256:
                raise StoreCorruptionError(
                    f"backup {target_id:06d}: assembled image digest does not"
                    f" match the manifest")
            checks.append(BackupCheck(
                backup_id=target_id, ok=True, detail="ok",
                layers=len(chain), chunks_verified=target.chunk_count,
                bytes_verified=total))

        referenced = {entry.sha256 for m in manifests for entry in m.chunks}
        unreferenced = sum(1 for digest, _p, _s in store.iter_chunks()
                           if digest not in referenced)
        return CheckReport(checks=tuple(checks),
                           unreferenced_chunks=unreferenced)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RestoreReport:
    backup_id: int
    target: str
    image_size: int
    image_sha256: str
    layers: tuple           # tuple[int, ...] chain ids, target first
    integrity_check: str


def _run_integrity_check(path: Path) -> str:
    """SQLite's own integrity check on the assembled image (read-only)."""
    from urllib.parse import quote
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise StoreCorruptionError(
            f"restored image {path} cannot be opened by SQLite: {exc}") from exc
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise StoreCorruptionError(
            f"integrity_check failed to run on {path}: {exc}") from exc
    finally:
        conn.close()
    problems = [row[0] for row in rows if row[0] != "ok"]
    if problems:
        raise StoreCorruptionError(
            f"SQLite integrity_check of {path} reported:"
            f" {'; '.join(problems[:5])}")
    return "ok"


def _wait_at_pause_gate(gate: str) -> None:
    """Drill/testing hook: after the lock is held and the dependency chain is
    resolved, signal ``<gate>.acquired`` and wait for ``<gate>.go``."""
    acquired = Path(gate + ".acquired")
    go = Path(gate + ".go")
    acquired.touch()
    deadline = time.monotonic() + PAUSE_GATE_TIMEOUT_SECONDS
    while not go.exists():
        if time.monotonic() > deadline:
            raise InvocationError(
                f"pause gate {gate}: timed out waiting for {go.name}")
        time.sleep(0.05)


def restore_backup(store: BackupStore, backup_id: int, target: str,
                   replace: bool = False,
                   pause_gate: Optional[str] = None) -> RestoreReport:
    """Assemble backup ``backup_id`` at ``target`` and prove it is sound.

    The image is built in a private staging file next to the target; only
    after every chunk, the whole-image digest and SQLite's integrity check
    pass is it atomically renamed over the target. Without ``replace`` an
    existing target is refused; with ``replace`` any failure leaves the
    original target bytes untouched.
    """
    target_path = Path(target)
    store_root = store.root
    try:
        real_target = os.path.realpath(
            target_path if not target_path.exists()
            else target_path)
    except OSError:
        real_target = str(target_path)
    if os.path.commonpath([store_root, os.path.abspath(real_target)]) == str(store_root):
        raise InvocationError(
            f"restore target {target} lies inside the backup directory"
            f" {store_root}")
    if target_path.exists() and not replace:
        raise InvocationError(
            f"target {target} already exists (pass --replace to overwrite)")
    if target_path.is_symlink():
        raise InvocationError(f"target {target} is a symlink; refusing")

    with store.locked(exclusive=False):
        store.check_layout()
        if not store.manifest_exists(backup_id):
            raise InvocationError(
                f"backup {backup_id:06d} does not exist in {store.root}")
        chain = load_chain(store, backup_id)
        resolution = resolve_chunks(chain)
        head = chain[0]

        if pause_gate is not None:
            _wait_at_pause_gate(pause_gate)

        staging = target_path.parent / f".{target_path.name}.restore-{os.getpid()}"
        try:
            image_hasher = hashlib.sha256()
            written = 0
            try:
                fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o644)
            except OSError as exc:
                raise TargetWriteError(
                    f"cannot create restore staging file {staging}:"
                    f" {exc}") from exc
            try:
                for index in range(head.chunk_count):
                    _layer, entry = resolution[index]
                    data = store.read_chunk(entry.sha256, entry.length)
                    try:
                        view = memoryview(data)
                        while view:
                            view = view[os.write(fd, view):]
                    except OSError as exc:
                        raise TargetWriteError(
                            f"cannot write restore staging file {staging}:"
                            f" {exc}") from exc
                    image_hasher.update(data)
                    written += len(data)
                try:
                    os.fsync(fd)
                except OSError as exc:
                    raise TargetWriteError(
                        f"cannot fsync restore staging file {staging}:"
                        f" {exc}") from exc
            finally:
                os.close(fd)

            if written != head.image_size:
                raise StoreCorruptionError(
                    f"backup {backup_id:06d}: assembled {written} bytes,"
                    f" manifest says {head.image_size}")
            assembled_digest = image_hasher.hexdigest()
            if assembled_digest != head.image_sha256:
                raise StoreCorruptionError(
                    f"backup {backup_id:06d}: assembled image digest"
                    f" {assembled_digest} does not match manifest"
                    f" {head.image_sha256}")

            integrity = _run_integrity_check(staging)

            # Re-verify after the integrity check: nothing may have changed
            # the bytes we are about to hand over.
            final_hasher = hashlib.sha256()
            with open(staging, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    final_hasher.update(block)
            if final_hasher.hexdigest() != head.image_sha256:
                raise StoreCorruptionError(
                    f"restored image changed during verification of backup"
                    f" {backup_id:06d}")

            if not replace and target_path.exists():
                raise InvocationError(
                    f"target {target} appeared while restoring; refusing to"
                    f" overwrite")
            try:
                os.replace(staging, target_path)
            except OSError as exc:
                raise TargetWriteError(
                    f"cannot move restored image into place at"
                    f" {target_path}: {exc}") from exc
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    return RestoreReport(
        backup_id=backup_id,
        target=str(target_path),
        image_size=head.image_size,
        image_sha256=head.image_sha256,
        layers=tuple(m.id for m in chain),
        integrity_check=integrity,
    )


# ---------------------------------------------------------------------------
# prune (retention)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrunePlan:
    keep: tuple                 # tuple[int, ...] requested keep ids
    needed_dependencies: tuple  # tuple[int, ...] ancestors not in keep
    removable_manifests: tuple  # tuple[int, ...]
    removable_chunks: tuple     # tuple[(digest, size), ...]
    unreferenced_chunks: tuple  # tuple[(digest, size), ...] subset of removable
    staging_files: tuple        # tuple[str, ...] leftover staging paths
    bytes_freed: int


def _compute_plan(store: BackupStore, keep_ids: tuple[int, ...]) -> PrunePlan:
    manifests = store.load_all_manifests()
    chains = _validate_graph(manifests)
    by_id = {m.id: m for m in manifests}

    unknown = [k for k in keep_ids if k not in by_id]
    if unknown:
        raise InvocationError(
            f"keep id(s) {', '.join(f'{k:06d}' for k in unknown)} do not"
            f" exist in {store.root}")

    # Prune is the only destructive operation: before anything is deleted,
    # every chunk referenced by any manifest must be present and sound. A
    # damaged store refuses destructive action instead of "guessing" a plan.
    expected_length: dict[str, int] = {}
    for manifest in manifests:
        for entry in manifest.chunks:
            previous = expected_length.setdefault(entry.sha256, entry.length)
            if previous != entry.length:
                raise StoreCorruptionError(
                    f"chunk {entry.sha256} is referenced with length"
                    f" {entry.length} by backup {manifest.id:06d} but with"
                    f" length {previous} elsewhere")
    for digest, length in expected_length.items():
        store.read_chunk(digest, length)

    needed: set[int] = set()
    for keep_id in keep_ids:
        needed.update(m.id for m in chains[keep_id])
    removable_manifests = tuple(sorted(set(by_id) - needed))
    dependencies = tuple(sorted(needed - set(keep_ids)))

    needed_chunks: set[str] = set()
    for needed_id in needed:
        resolution = resolve_chunks(chains[needed_id])
        needed_chunks.update(entry.sha256 for _l, entry in resolution.values())

    on_disk = store.iter_chunks()
    referenced = {entry.sha256 for m in manifests for entry in m.chunks}
    removable = [(digest, size) for digest, _p, size in on_disk
                 if digest not in needed_chunks]
    unreferenced = [(digest, size) for digest, _p, size in on_disk
                    if digest not in referenced]
    staging = tuple(str(p) for p in store.iter_staging())
    freed = sum(size for _d, size in removable)
    return PrunePlan(
        keep=tuple(sorted(keep_ids)),
        needed_dependencies=dependencies,
        removable_manifests=removable_manifests,
        removable_chunks=tuple(sorted(removable)),
        unreferenced_chunks=tuple(sorted(unreferenced)),
        staging_files=staging,
        bytes_freed=freed,
    )


def plan_prune(store: BackupStore, keep_ids: tuple[int, ...]) -> PrunePlan:
    """Preview retention: what stays, what would be freed. Deletes nothing."""
    with store.locked(exclusive=False):
        store.check_layout()
        return _compute_plan(store, keep_ids)


def apply_prune(store: BackupStore, keep_ids: tuple[int, ...]) -> PrunePlan:
    """Apply retention under the exclusive lock.

    The plan is recomputed inside the lock (a preview is never trusted).
    Manifests go first: a crash then leaves unreferenced chunks that a later
    prune recognises, never a manifest pointing at deleted chunks. Any
    corruption (missing parent, cycle, bad field, bad reference, symlink)
    aborts before anything is deleted.
    """
    with store.locked(exclusive=True):
        store.check_layout()
        plan = _compute_plan(store, keep_ids)
        for backup_id in plan.removable_manifests:
            store.remove_manifest(backup_id)
        for digest, _size in plan.removable_chunks:
            store.remove_chunk(digest)
        for staging in plan.staging_files:
            store.remove_staging(Path(staging))
        return plan
