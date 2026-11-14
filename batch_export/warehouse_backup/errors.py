"""Error types and process exit codes for the warehouse backup tool.

Three failure classes let shell automation tell a bad command line apart from
a damaged backup store and from a failure while writing the restore target:

* :class:`InvocationError` -- the caller asked for something impossible:
  unknown backup id, missing source database, target exists without
  ``--replace``, unknown ``--keep`` id. Nothing was read or written beyond
  parsing the request.
* :class:`StoreCorruptionError` -- the backup directory itself is broken:
  invalid manifest, missing parent, parent cycle, missing/corrupt chunk,
  symlink substitution, image digest mismatch, failed SQLite integrity check.
  The tool never repairs the store and never falls back to an older backup.
* :class:`TargetWriteError` -- the store is fine but the output side failed
  (staging file, fsync, atomic rename). A pre-existing target keeps its
  original bytes in this case.
"""

# Process exit codes, deliberately distinct so scripts can react per class.
EXIT_OK = 0
EXIT_INVOCATION = 2
EXIT_CORRUPTION = 3
EXIT_TARGET_WRITE = 4


class BackupError(Exception):
    """Base class for every backup-tool failure."""

    exit_code = EXIT_INVOCATION
    category = "invocation"


class InvocationError(BackupError):
    """The request itself is invalid (unknown id, bad path, refused replace)."""

    exit_code = EXIT_INVOCATION
    category = "invocation"


class StoreCorruptionError(BackupError):
    """The backup store failed validation; nothing may be deleted or trusted."""

    exit_code = EXIT_CORRUPTION
    category = "corruption"


class TargetWriteError(BackupError):
    """Writing the restore target (or staging image) failed."""

    exit_code = EXIT_TARGET_WRITE
    category = "target-write"
