"""Parser for ``META-INF/taxonomyPackage.xml`` (Taxonomy Package 1.0).

Elements are matched by the standard namespace URI and local name only; a
document that renames the ``tp:`` prefix is parsed identically. Only the four
standard pieces of information this tool needs are accepted: identifier,
name, version and entryPointDocument hrefs (with their folded xml:base).
"""
from __future__ import annotations

from lxml import etree

from .errors import PackageFormatError
from .models import TP_NS, EntryPointSpec, PackageDescriptor
from .uriutil import (
    is_logical_uri,
    join_uri,
    logical_uri_to_member_path,
    strip_fragment,
)
from .xmlutil import (
    child_elements,
    effective_base,
    is_qname,
    local_of,
    ns_of,
    parse_xml_bytes,
    require_attr,
)


def parse_descriptor_document(data: bytes, document_uri: str,
                              entry_base_uri: str,
                              package_sha256: str) -> PackageDescriptor:
    """Parse the descriptor.

    ``document_uri`` identifies the descriptor file in error messages;
    ``entry_base_uri`` is the logical URI of the *package root* -- per
    Taxonomy Package 1.0, ``entryPointDocument`` hrefs are relative references
    resolved against the directory that contains ``META-INF``, with any
    effective ``xml:base`` folded on top.
    """
    root = parse_xml_bytes(data, document_uri)
    if not is_qname(root, TP_NS, "taxonomyPackage"):
        raise PackageFormatError(
            f"{document_uri}: root element must be {{{TP_NS}}}taxonomyPackage,"
            f" found {root.tag!r}")

    identifier = name = version = None
    entry_points: list[EntryPointSpec] = []

    for child in child_elements(root):
        if ns_of(child.tag) != TP_NS:
            raise PackageFormatError(
                f"{document_uri}:{child.sourceline}: foreign element"
                f" {child.tag!r} is not supported in a taxonomy package"
                " descriptor")
        local = local_of(child.tag)
        if local == "identifier" and identifier is None:
            identifier = _text(child, document_uri)
        elif local == "name" and name is None:
            name = _text(child, document_uri)
        elif local == "version" and version is None:
            version = _text(child, document_uri)
        elif local == "entryPoints":
            for entry in child_elements(child):
                if not is_qname(entry, TP_NS, "entryPoint"):
                    raise PackageFormatError(
                        f"{document_uri}:{entry.sourceline}: unexpected element"
                        f" {entry.tag!r} inside entryPoints")
                for sub in child_elements(entry):
                    if is_qname(sub, TP_NS, "entryPointDocument"):
                        href = require_attr(sub, "href", document_uri)
                        # xml:base chains start at the package root, not at
                        # the descriptor file's META-INF directory.
                        base = effective_base(sub, entry_base_uri)
                        _check_entry_base(base, href, document_uri, sub)
                        entry_points.append(EntryPointSpec(
                            href=href,
                            effective_base=base,
                            line=int(sub.sourceline or 0),
                        ))
                    elif ns_of(sub.tag) == TP_NS:
                        # entryPoint/name and entryPoint/entryPointFolder etc.
                        # are tolerated as long as they carry no documents.
                        continue
                    else:
                        raise PackageFormatError(
                            f"{document_uri}:{sub.sourceline}: foreign element"
                            f" {sub.tag!r} inside entryPoint is not supported")
        else:
            raise PackageFormatError(
                f"{document_uri}:{child.sourceline}: unsupported or duplicated"
                f" descriptor element {local!r}")

    missing = [label for label, value in
               (("identifier", identifier), ("name", name), ("version", version))
               if value is None]
    if missing:
        raise PackageFormatError(
            f"{document_uri}: descriptor is missing required element(s)"
            f" {', '.join(missing)} (package {package_sha256[:12]})")
    if not entry_points:
        raise PackageFormatError(
            f"{document_uri}: descriptor declares no entryPointDocument")

    return PackageDescriptor(
        identifier=identifier,
        name=name,
        version=version,
        entry_points=tuple(entry_points),
    )


def _text(elem: etree._Element, document_uri: str) -> str:
    text = (elem.text or "").strip()
    if not text:
        raise PackageFormatError(
            f"{document_uri}:{elem.sourceline}: {local_of(elem.tag)} must have"
            " non-empty text content")
    return text


def _check_entry_base(base: str, href: str, document_uri: str,
                      elem: etree._Element) -> None:
    """An entry href must ultimately stay inside this package's logical space."""
    absolute = strip_fragment(join_uri(base, href))
    if not is_logical_uri(absolute):
        raise PackageFormatError(
            f"{document_uri}:{elem.sourceline}: entryPointDocument href"
            f" {href!r} resolves to external URI {absolute!r}; entries must"
            " point into the package")
    # Percent-decoded containment: %2e%2e style escapes are rejected too.
    where = f"{document_uri}:{elem.sourceline}"
    try:
        logical_uri_to_member_path(absolute, where=where)
    except PackageFormatError as exc:
        raise PackageFormatError(
            f"{document_uri}:{elem.sourceline}: entryPointDocument href"
            f" {href!r} escapes the package root: {exc}") from exc
