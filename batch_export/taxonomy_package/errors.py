"""Error hierarchy and process exit codes for the taxonomy package registry.

Five distinct exit codes let shell automation separate a bad command line
from a malformed package, a broken resolution, registry corruption and an
output failure:

* 0  success;
* 2  invocation error (bad arguments, unknown package/entry, refused
     overwrite); nothing was written;
* 3  registry corruption (missing member, changed bytes, inconsistent URI
     resolution tables); the on-disk registry is never repaired silently;
* 4  package/parsing error (unsafe ZIP member, bad descriptor/catalog, DTD
     or external entity, unsupported reference, missing dependency,
     rewrite conflict, ambiguous URI); registry generation is refused;
* 5  atomic publish / filesystem write failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

EXIT_OK = 0
EXIT_INVOCATION = 2
EXIT_CORRUPTION = 3
EXIT_PACKAGE = 4
EXIT_WRITE = 5


class TaxonomyError(Exception):
    """Base class for every taxonomy-package failure."""

    exit_code = EXIT_INVOCATION
    category = "invocation"


class InvocationError(TaxonomyError):
    """The request itself is invalid; nothing was read or written."""

    exit_code = EXIT_INVOCATION
    category = "invocation"


class PackageFormatError(TaxonomyError):
    """The ZIP, descriptor or catalog does not satisfy the supported format."""

    exit_code = EXIT_PACKAGE
    category = "package"


class VersionConflictError(TaxonomyError):
    """Same package identifier+version already exists with different bytes."""

    exit_code = EXIT_PACKAGE
    category = "version-conflict"


class CatalogConflictError(TaxonomyError):
    """Two catalog rules disagree, or a URI resolves ambiguously."""

    exit_code = EXIT_PACKAGE
    category = "catalog-conflict"


class ResolutionError(TaxonomyError):
    """An entry schema cannot be resolved within the registered package."""

    exit_code = EXIT_PACKAGE
    category = "resolution"


class RegistryCorruptionError(TaxonomyError):
    """A previously published registry no longer matches its recorded state."""

    exit_code = EXIT_CORRUPTION
    category = "corruption"


class PublishError(TaxonomyError):
    """Atomic staging or publishing of the registry failed."""

    exit_code = EXIT_WRITE
    category = "publish"


@dataclass(frozen=True)
class SourceLocation:
    """Where a reference was seen: the logical document URI and 1-based line.

    lxml only exposes a source line (not a column) for parsed elements, so
    ``column`` is 0 when unknown and omitted from the rendered text then.
    """

    document_uri: str
    line: int
    column: int = 0

    def __str__(self) -> str:
        if self.column:
            return f"{self.document_uri}:{self.line}:{self.column}"
        return f"{self.document_uri}:{self.line}"


def located(message: str, location: Optional[SourceLocation]) -> str:
    """Render a resolution message with the originating file and line."""
    if location is None:
        return message
    return f"{location}: {message}"
