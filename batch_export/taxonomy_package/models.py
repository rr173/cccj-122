"""Namespace constants and immutable value types for the taxonomy registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Standard namespaces. Elements are always matched by namespace URI + local
# name, never by the XML prefix a document happens to choose.
TP_NS = "http://xbrl.org/2016/taxonomy-package"
CATALOG_NS = "urn:oasis:names:tc:entity:xmlns:xml:catalog"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
XLINK_NS = "http://www.w3.org/1999/xlink"
XBRL_LINK_NS = "http://www.xbrl.org/2003/linkbase"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# Logical URIs for members inside a registered package use this private
# scheme so they can never be mistaken for public, downloadable URIs.
PKG_SCHEME = "xbrl-pkg"
PKG_SCHEME_PREFIX = PKG_SCHEME + "://"

META_INF = "META-INF"
DESCRIPTOR_REL = META_INF + "/taxonomyPackage.xml"
CATALOG_REL = META_INF + "/catalog.xml"
REGISTRY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PackageMember:
    """One regular-file ZIP member, normalised to forward-slash relative form."""

    path: str                 # normalised member path relative to package root
    sha256: str
    byte_size: int
    is_xml: bool


@dataclass(frozen=True)
class EntryPointSpec:
    """One entryPointDocument as declared, with its effective xml:base.

    ``href`` is the raw attribute text; ``effective_base`` is the folded
    xml:base chain at that element (absolute logical URI of the descriptor,
    possibly extended by relative xml:base segments), or None.
    """

    href: str
    effective_base: Optional[str]
    line: int


@dataclass(frozen=True)
class PackageDescriptor:
    """The four standard pieces of information from taxonomyPackage.xml."""

    identifier: str
    name: str
    version: str
    entry_points: tuple[EntryPointSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CatalogRule:
    """One OASIS rewriteURI rule, resolved against the catalog location."""

    uri_start_string: str
    rewrite_prefix: str          # already resolved to an xbrl-pkg logical URI
    rewrite_prefix_raw: str      # original attribute text, kept for auditing
    line: int


@dataclass(frozen=True)
class LoadedPackage:
    """Everything learned from one ZIP without touching the target registry."""

    package_sha256: str
    top_level_dir: str
    descriptor: PackageDescriptor
    catalog_rules: tuple[CatalogRule, ...]
    members: tuple[PackageMember, ...]
    entry_member_paths: tuple[str, ...]
    entry_logical_uris: tuple[str, ...]
    # Byte payloads are held separately (a mapping kept by the loader) rather
    # than copied into this frozen value.


@dataclass(frozen=True)
class RegisterResult:
    """Outcome of registering one ZIP against one registry."""

    package_sha256: str
    identifier: str
    version: str
    reused: bool
    package_dir: str
    member_count: int
    entry_count: int


@dataclass(frozen=True)
class ReferenceRecord:
    """One raw reference discovered while scanning a schema/linkbase file."""

    kind: str                 # import | include | redefine | linkbaseRef
    raw_href: str
    effective_base: str
    line: int


@dataclass(frozen=True)
class ResolutionFile:
    """One file in a resolved closure, keyed by its public/logical URI."""

    public_uri: str
    member_path: str
    member_sha256: str
    byte_size: int
    is_entry: bool
    is_xml: bool


@dataclass(frozen=True)
class ResolutionEdge:
    """One dependency edge between two resolved files (cycles included)."""

    source_uri: str
    target_uri: str
    kind: str
    raw_href: str
    is_cycle: bool


@dataclass(frozen=True)
class ResolutionBatch:
    """The full resolution of one requested set of entry points."""

    batch_id: int
    entry_uris: tuple[str, ...]
    entry_member_paths: tuple[str, ...]
    files: tuple[ResolutionFile, ...]
    edges: tuple[ResolutionEdge, ...]
    file_count: int
    edge_count: int
    cycle_edge_count: int


@dataclass(frozen=True)
class VerifyReport:
    """Result of a full offline verification pass."""

    ok: bool
    package_count: int
    members_verified: int
    bytes_verified: int
    batches_verified: int
    problems: tuple[str, ...] = field(default_factory=tuple)
