"""Strict reader for XBRL Taxonomy Package 1.0 ZIP archives.

A package is accepted only when every member is a regular file whose
forward-slash name sits below one common top-level directory containing
``META-INF/taxonomyPackage.xml`` (and, for this tool, ``META-INF/catalog.xml``).
Absolute names, drive letters, ``..`` segments, duplicate normalised names and
Unix symlink members are refused, so nothing extracted from the archive can
ever be written outside the registry's package directory.
"""
from __future__ import annotations

import hashlib
import posixpath
import stat
import zipfile
from dataclasses import dataclass
from typing import Optional

from . import uriutil
from .errors import PackageFormatError
from .models import (
    CATALOG_REL,
    DESCRIPTOR_REL,
    META_INF,
    CatalogRule,
    LoadedPackage,
    PackageDescriptor,
    PackageMember,
)
from .xmlutil import looks_like_xml, reject_dtd


@dataclass
class _RawMember:
    name: str           # exactly as stored in the ZIP
    info: zipfile.ZipInfo
    data: bytes


def _validate_member_name(name: str) -> None:
    if not name or name.endswith("/"):
        # Directory entries are filtered before this is called; anything else
        # empty is invalid.
        raise PackageFormatError(f"ZIP member has an empty name")
    if "\x00" in name:
        raise PackageFormatError("ZIP member name contains a NUL byte")
    if "\\" in name:
        raise PackageFormatError(
            f"ZIP member {name!r} uses backslashes; ZIP paths must use '/'")
    if name.startswith("/"):
        raise PackageFormatError(
            f"ZIP member {name!r} is an absolute path and is refused")
    if any(len(part) >= 2 and part[1] == ":" and part[0].isalpha()
           for part in name.split("/")):
        raise PackageFormatError(
            f"ZIP member {name!r} is a drive-letter path and is refused")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PackageFormatError(
            f"ZIP member {name!r} contains '.', '..' or empty path segments;"
            " traversal outside the package is refused")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _common_top_level(names: list[str]) -> str:
    tops = {name.split("/", 1)[0] for name in names}
    if not tops:
        raise PackageFormatError("the ZIP archive contains no members")
    if len(tops) != 1:
        raise PackageFormatError(
            "package members do not share one common top-level directory;"
            f" found {sorted(tops)!r}")
    top = tops.pop()
    if top == META_INF:
        raise PackageFormatError(
            "META-INF must sit below the common top-level package directory,"
            " not at the archive root")
    return top


def load_package_zip(zip_path: str) -> tuple[LoadedPackage, dict[str, bytes]]:
    """Read and validate one taxonomy package ZIP.

    Returns the structured package plus a ``member path -> bytes`` payload
    mapping (the payload is kept out of the frozen value so registrations of
    large packages do not duplicate it through dataclass copies).
    """
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageFormatError(f"cannot open {zip_path!r} as a ZIP: {exc}") from exc

    with archive:
        infolist = archive.infolist()
        names = [info.filename for info in infolist if not info.is_dir()]
        top = _common_top_level(names)

        seen: set[str] = set()
        raw: list[_RawMember] = []
        package_hasher = hashlib.sha256()
        for info in infolist:
            name = info.filename
            if info.is_dir():
                if _is_symlink(info):
                    raise PackageFormatError(
                        f"ZIP member {name!r} is a symbolic link and is refused")
                # Directory names are validated too: a traversal segment is
                # refused even when no regular file uses that entry.
                _validate_member_name(name.rstrip("/"))
                continue
            _validate_member_name(name)
            if _is_symlink(info):
                raise PackageFormatError(
                    f"ZIP member {name!r} is a symbolic link and is refused")
            normalised = posixpath.normpath(name)
            if normalised in seen:
                raise PackageFormatError(
                    f"ZIP contains duplicate normalised member path {normalised!r}")
            seen.add(normalised)
            try:
                data = archive.read(info)  # raises BadZipFile on CRC failure
            except zipfile.BadZipFile as exc:
                raise PackageFormatError(
                    f"ZIP member {name!r} is corrupt (CRC/decompress failure): {exc}") \
                    from exc
            package_hasher.update(data)
            raw.append(_RawMember(name=normalised, info=info, data=data))

    prefix = top + "/"
    payloads: dict[str, bytes] = {}
    members: list[PackageMember] = []
    descriptor_path: Optional[str] = None
    catalog_path: Optional[str] = None
    for item in raw:
        if not item.name.startswith(prefix):
            raise PackageFormatError(
                f"member {item.name!r} is outside the common top-level directory"
                f" {top!r}")
        rel = item.name[len(prefix):]
        if not rel:
            raise PackageFormatError(
                f"member {item.name!r} has no path below the top-level directory")
        payloads[rel] = item.data
        is_xml = looks_like_xml(item.data)
        if is_xml:
            # Security screening applies to every XML member, not only to
            # the two META-INF documents: a DTD or entity declaration in a
            # schema or linkbase is rejected at registration time.
            reject_dtd(item.data, f"{top}/{rel}")
        members.append(PackageMember(
            path=rel,
            sha256=hashlib.sha256(item.data).hexdigest(),
            byte_size=len(item.data),
            is_xml=is_xml,
        ))
        if rel == DESCRIPTOR_REL:
            descriptor_path = rel
        if rel == CATALOG_REL:
            catalog_path = rel

    if descriptor_path is None:
        raise PackageFormatError(
            f"package is missing {prefix}{DESCRIPTOR_REL}")
    if catalog_path is None:
        raise PackageFormatError(
            f"package is missing {prefix}{CATALOG_REL}; an OASIS catalog is"
            " required for offline URI resolution")

    package_sha256 = package_hasher.hexdigest()
    descriptor_base = uriutil.member_logical_uri(descriptor_path)
    package_root_uri = uriutil.member_logical_uri("").rstrip("/") + "/"
    descriptor = _parse_descriptor(
        payloads[descriptor_path], descriptor_base, package_root_uri,
        package_sha256)
    catalog_base = uriutil.member_logical_uri(catalog_path)
    rules = _parse_catalog(payloads[catalog_path], catalog_base)

    entry_member_paths = tuple(
        _entry_to_member_path(entry) for entry in descriptor.entry_points
    )
    # The loader has already folded xml:base and containment for every entry
    # href; derive their canonical logical URIs from those member paths so the
    # catalog never inherits the descriptor's META-INF base directory.
    entry_logical_uris = tuple(
        uriutil.member_logical_uri(path) for path in entry_member_paths
    )

    package = LoadedPackage(
        package_sha256=package_sha256,
        top_level_dir=top,
        descriptor=descriptor,
        catalog_rules=rules,
        members=tuple(sorted(members, key=lambda m: m.path)),
        entry_member_paths=entry_member_paths,
        entry_logical_uris=entry_logical_uris,
    )
    return package, payloads


def _entry_to_member_path(entry) -> str:
    """Resolve an entryPointDocument href to its in-package member path."""
    absolute = uriutil.join_uri(entry.effective_base, entry.href)
    try:
        return uriutil.logical_uri_to_member_path(
            uriutil.strip_fragment(absolute),
            where=f"entryPointDocument at line {entry.line}",
        )
    except PackageFormatError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise PackageFormatError(
            f"entryPointDocument at line {entry.line}: href {entry.href!r} does"
            f" not resolve inside the package: {exc}") from exc


# ---------------------------------------------------------------------------
# taxonomyPackage.xml
# ---------------------------------------------------------------------------

def _parse_descriptor(data: bytes, document_base: str,
                      package_root_uri: str,
                      package_sha256: str) -> PackageDescriptor:
    from .descriptor import parse_descriptor_document
    return parse_descriptor_document(
        data, document_base, package_root_uri, package_sha256)


def _parse_catalog(data: bytes, document_base: str) -> tuple[CatalogRule, ...]:
    from .catalogdoc import parse_catalog_document
    return parse_catalog_document(data, document_base)
