"""Parser for ``META-INF/catalog.xml`` (OASIS XML Catalog, rewriteURI only).

The supported subset is deliberately narrow: the ``catalog`` root and its
``rewriteURI`` children (including rewriteURI children wrapped in the
standard ``group`` element). Any other OASIS rule (``uri``, ``public``,
``system``, ``rewriteSystem``, ``nextCatalog``, ...) or any element from a
foreign namespace is an explicit error -- unknown rules are never silently
ignored. Effective ``xml:base`` attributes are folded into each rule.
"""
from __future__ import annotations

from lxml import etree

from .errors import CatalogConflictError, PackageFormatError
from .models import CATALOG_NS, CatalogRule
from .uriutil import (
    assert_logical_uri_contained,
    is_logical_uri,
    join_uri,
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

_SUPPORTED_CATALOG_LOCALS = frozenset({"catalog", "group", "rewriteURI"})


def parse_catalog_document(data: bytes,
                           document_base: str) -> tuple[CatalogRule, ...]:
    root = parse_xml_bytes(data, document_base)
    if not is_qname(root, CATALOG_NS, "catalog"):
        raise PackageFormatError(
            f"{document_base}: root element must be {{{CATALOG_NS}}}catalog,"
            f" found {root.tag!r}")

    rules: list[CatalogRule] = []
    _scan(root, document_base, rules)

    # Same uriStartString must always map to the same resolved target. Two
    # different targets at equal prefix length would make resolution
    # ambiguous, so the catalog is refused up front.
    by_prefix: dict[str, CatalogRule] = {}
    for rule in rules:
        existing = by_prefix.get(rule.uri_start_string)
        if existing is not None:
            if existing.rewrite_prefix != rule.rewrite_prefix:
                raise CatalogConflictError(
                    f"{document_base}: rewriteURI rules with the same"
                    f" uriStartString {rule.uri_start_string!r} target different"
                    f" prefixes: {existing.rewrite_prefix!r} (line"
                    f" {existing.line}) vs {rule.rewrite_prefix!r} (line"
                    f" {rule.line})")
            # Fully identical rule repeated: still rejected rather than
            # silently de-duplicated, so the catalog's intent stays explicit.
            raise PackageFormatError(
                f"{document_base}: duplicate rewriteURI rule for"
                f" uriStartString {rule.uri_start_string!r} at line"
                f" {rule.line}")
        by_prefix[rule.uri_start_string] = rule
    return tuple(rules)


def _scan(elem: etree._Element, document_base: str,
          rules: list[CatalogRule]) -> None:
    for child in child_elements(elem):
        ns = ns_of(child.tag)
        local = local_of(child.tag)
        if ns != CATALOG_NS:
            raise PackageFormatError(
                f"{document_base}:{child.sourceline}: foreign element"
                f" {child.tag!r} in the OASIS catalog is not supported")
        if local not in _SUPPORTED_CATALOG_LOCALS:
            raise PackageFormatError(
                f"{document_base}:{child.sourceline}: OASIS catalog rule"
                f" {local!r} is not supported by this tool (only rewriteURI is;"
                " unknown rules are refused, not ignored)")
        if local == "rewriteURI":
            rules.append(_build_rule(child, document_base))
        else:
            _scan(child, document_base, rules)


def _build_rule(elem: etree._Element, document_base: str) -> CatalogRule:
    start = require_attr(elem, "uriStartString", document_base)
    raw_prefix = require_attr(elem, "rewritePrefix", document_base)
    base = effective_base(elem, document_base)
    target = join_uri(base, raw_prefix)
    if not is_logical_uri(target):
        raise PackageFormatError(
            f"{document_base}:{elem.sourceline}: rewritePrefix {raw_prefix!r}"
            f" resolves to {target!r}, which is not an in-package"
            " xbrl-pkg URI; rewrites must point inside the package")
    # The resolved rewrite prefix must itself be a contained package path.
    # Escapes hidden behind percent encoding are caught here; a later match
    # appending a public-URI suffix is re-checked again at resolution time.
    try:
        assert_logical_uri_contained(
            target, where=f"catalog line {elem.sourceline}")
    except PackageFormatError as exc:
        raise PackageFormatError(
            f"{document_base}:{elem.sourceline}: rewritePrefix {raw_prefix!r}"
            f" is not a contained package path: {exc}") from exc
    if "://" not in start or ":" not in start:
        raise PackageFormatError(
            f"{document_base}:{elem.sourceline}: uriStartString {start!r} must"
            " be an absolute public URI prefix")
    return CatalogRule(
        uri_start_string=start,
        rewrite_prefix=target,
        rewrite_prefix_raw=raw_prefix,
        line=int(elem.sourceline or 0),
    )
