"""Hardened XML loading and small namespace/xml:base helpers.

Every XML byte stream the tool parses goes through :func:`parse_xml_bytes`.
The lxml parser is configured with networking, DTD loading and entity
expansion switched off, and the raw bytes are additionally scanned for
``<!DOCTYPE`` / ``<!ENTITY`` declarations so a document carrying a DTD or an
external entity is refused even if a parser release were lenient about it.
"""
from __future__ import annotations

from typing import Iterable

from lxml import etree

from .errors import PackageFormatError, SourceLocation


class XmlSecurityError(PackageFormatError):
    """A document contains a DTD, an entity declaration or another banned form."""


def hardened_parser() -> etree.XMLParser:
    """Parser that never fetches the network, loads a DTD or expands entities."""
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        dtd_validation=False,
        no_network=True,
        huge_tree=False,
        collect_ids=False,
    )


def reject_dtd(data: bytes, where: str) -> None:
    """Reject any DTD / entity declaration before the parser even sees it."""
    # XML keyword declarations are uppercase per the XML specification;
    # scanning bytes avoids any decoder ambiguity.
    for marker in (b"<!DOCTYPE", b"<!ENTITY"):
        index = data.find(marker)
        if index != -1:
            line = data.count(b"\n", 0, index) + 1
            raise XmlSecurityError(
                f"{where}:{line}: document contains a {marker[2:].decode()} declaration;"
                " DTDs, internal subsets and external entities are not supported")
    if b"&#x" in data or b"&#" in data:
        # Numeric character references are legal XML; nothing to do here. This
        # branch intentionally exists to document that they remain allowed.
        pass


def parse_xml_bytes(data: bytes, document_uri: str) -> etree._Element:
    """Parse XML bytes with the full hardening stack.

    Raises :class:`XmlSecurityError` for DTD/entity content and
    :class:`PackageFormatError` for any other well-formedness failure.
    """
    reject_dtd(data, document_uri)
    parser = hardened_parser()
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise PackageFormatError(
            f"{document_uri}: document is not well-formed XML: {exc}") from exc
    # Defence in depth: lxml docinfo must never report a loaded DTD.
    docinfo = getattr(root, "docinfo", None)
    if docinfo is not None and (docinfo.internalDTD or docinfo.externalDTD):
        raise XmlSecurityError(
            f"{document_uri}: document is bound to a DTD, which is not supported")
    return root


def ns_of(tag: str) -> str:
    """Namespace URI of a Clark-notation lxml tag ('' when unqualified)."""
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def local_of(tag: str) -> str:
    """Local name of a Clark-notation lxml tag."""
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def is_qname(elem, namespace: str, local_name: str) -> bool:
    """Namespace-URI + local-name match; the document's prefix is irrelevant."""
    return elem.tag == f"{{{namespace}}}{local_name}"


def child_elements(elem: etree._Element) -> Iterable[etree._Element]:
    """Direct element children only (comments and PIs are skipped)."""
    for child in elem:
        if isinstance(child.tag, str):
            yield child


def effective_base(elem: etree._Element, document_base: str) -> str:
    """Fold the effective xml:base chain from the document root down to ``elem``.

    Relative ``xml:base`` values are joined in document order onto the
    document's logical base with the same URI rules the rest of the tool
    uses (RFC 3986 for public URIs, POSIX path rules for the private
    ``xbrl-pkg`` scheme). The local filesystem is never consulted.
    """
    from .uriutil import join_uri
    chain = [elem]
    chain.extend(elem.iterancestors())
    base = document_base
    for ancestor in reversed(chain):
        value = ancestor.get(f"{{{_XML_NS}}}base")
        if value:
            base = join_uri(base, value)
    return base


def location_of(elem: etree._Element, document_uri: str) -> SourceLocation:
    """Source location for an error message (lxml supplies line only)."""
    return SourceLocation(document_uri=document_uri, line=int(elem.sourceline or 0))


_XML_NS = "http://www.w3.org/XML/1998/namespace"


def looks_like_xml(data: bytes) -> bool:
    """Cheap content sniff used to flag XML members without parsing them.

    Only members actually reached by a resolution are ever parsed; this flag
    merely lets the registry label every registered member. A BOM and leading
    whitespace are tolerated; anything that does not begin with an XML markup
    declaration or an element start is treated as non-XML.
    """
    head = data.lstrip()
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip()
    if head.startswith(b"<?xml"):
        return True
    if head.startswith(b"<") and len(head) >= 2:
        second = head[1:2]
        return second.isalpha() or second in (b"_", b"!")
    return False


def require_attr(elem: etree._Element, name: str, document_uri: str) -> str:
    """Read a required unqualified attribute or raise with its line."""
    value = elem.get(name)
    if value is None or value == "":
        raise PackageFormatError(
            f"{document_uri}:{elem.sourceline}: required attribute {name!r} is"
            " missing or empty")
    return value
