"""Consistent, deduplicated, restorable backups of the SQLite fact warehouse.

Read README.md in this package for the directory format, the chunk
inheritance rule, the offline-restore contract and the retention commands.
"""
from .errors import (
    EXIT_CORRUPTION,
    EXIT_INVOCATION,
    EXIT_OK,
    EXIT_TARGET_WRITE,
    BackupError,
    InvocationError,
    StoreCorruptionError,
    TargetWriteError,
)
from .format import CHUNK_SIZE, ChunkEntry, Manifest
from .service import (
    CreateReport,
    PrunePlan,
    RestoreReport,
    apply_prune,
    check_backups,
    create_backup,
    load_chain,
    plan_prune,
    resolve_chunks,
    restore_backup,
)
from .store import BackupStore

__all__ = [
    "BackupError",
    "BackupStore",
    "CHUNK_SIZE",
    "ChunkEntry",
    "CreateReport",
    "EXIT_CORRUPTION",
    "EXIT_INVOCATION",
    "EXIT_OK",
    "EXIT_TARGET_WRITE",
    "InvocationError",
    "Manifest",
    "PrunePlan",
    "RestoreReport",
    "StoreCorruptionError",
    "TargetWriteError",
    "apply_prune",
    "check_backups",
    "create_backup",
    "load_chain",
    "plan_prune",
    "resolve_chunks",
    "restore_backup",
]
