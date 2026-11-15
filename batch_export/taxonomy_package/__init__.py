"""Offline XBRL Taxonomy Package 1.0 registry and entry-schema resolver.

A ZIP that follows the XBRL Taxonomy Package 1.0 layout is registered as a
reusable offline directory: the original ZIP is content-addressed by its
SHA-256, every member is extracted and digest-checked, the OASIS XML Catalog
``rewriteURI`` rules are stored, and a chosen entry schema can be resolved to
the exact closure of package files it actually depends on.

The module never reads XBRL instance documents, never writes a fact
warehouse, never runs calculation checks and never opens a network socket.
Read README.md in this package for the full contract.
"""
from .errors import (
    EXIT_CORRUPTION,
    EXIT_INVOCATION,
    EXIT_OK,
    EXIT_PACKAGE,
    EXIT_WRITE,
    CatalogConflictError,
    InvocationError,
    PackageFormatError,
    PublishError,
    RegistryCorruptionError,
    ResolutionError,
    SourceLocation,
    TaxonomyError,
    VersionConflictError,
)
from .models import (
    CatalogRule,
    EntryPointSpec,
    PackageDescriptor,
    PackageMember,
    ReferenceRecord,
    RegisterResult,
    ResolutionBatch,
    ResolutionEdge,
    ResolutionFile,
    VerifyReport,
)
from .resolver import SchemaResolver
from .service import TaxonomyRegistry
from .store import REGISTRY_FORMAT_TEXT, TaxonomyStore
from .zippkg import LoadedPackage, load_package_zip

__all__ = [
    "CatalogConflictError",
    "CatalogRule",
    "EntryPointSpec",
    "EXIT_CORRUPTION",
    "EXIT_INVOCATION",
    "EXIT_OK",
    "EXIT_PACKAGE",
    "EXIT_WRITE",
    "LoadedPackage",
    "PackageDescriptor",
    "PackageFormatError",
    "PackageMember",
    "PublishError",
    "ReferenceRecord",
    "REGISTRY_FORMAT_TEXT",
    "RegisterResult",
    "RegistryCorruptionError",
    "ResolutionBatch",
    "ResolutionEdge",
    "ResolutionFile",
    "ResolutionError",
    "SchemaResolver",
    "SourceLocation",
    "TaxonomyError",
    "TaxonomyRegistry",
    "TaxonomyStore",
    "VerifyReport",
    "load_package_zip",
]
