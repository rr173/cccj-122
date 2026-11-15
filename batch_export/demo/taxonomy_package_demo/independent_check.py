"""Independent expectations for the taxonomy package demo.

This script deliberately does NOT import any ``taxonomy_package`` module. It
re-opens the demonstration ZIP with the standard-library ``zipfile`` and
``xml.etree.ElementTree`` only, and recomputes from first principles:

* every expected public/logical URI in the entry closure;
* the expected final package member for each URI;
* every expected dependency edge (including the cycle edge);
* the expected longest-prefix rewrite choice.

The demo drill compares these independently derived expectations against the
production registry's resolution output. If both disagree, either the
production parser or this independent reader is wrong, and the drill fails.
"""
from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import unquote, urlsplit

XSD_NS = "http://www.w3.org/2001/XMLSchema"
LINK_NS = "http://www.xbrl.org/2003/linkbase"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
CAT_NS = "urn:oasis:names:tc:entity:xmlns:xml:catalog"
PKG = "xbrl-pkg://package/"


def _norm(path: str) -> str:
    return posixpath.normpath(path)


def _join(base: str, ref: str) -> str:
    """Independent package-aware join (mirrors RFC 3986 path semantics).

    A base without a trailing slash denotes a file: its last path component
    is removed before the relative reference is joined. A trailing slash
    denotes a directory and is kept.
    """
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref.split("#", 1)[0]
    ref = ref.split("#", 1)[0]
    base_path = urlsplit(base).path
    if base_path.endswith("/"):
        directory = base_path
    else:
        parent = posixpath.dirname(base_path)
        directory = parent if parent.endswith("/") else parent + "/"
    target = _norm(directory + ref)
    if ref.endswith("/") and not target.endswith("/"):
        target += "/"
    return "xbrl-pkg://package" + target


def _fold_xml_base(elem, parent_map: dict, doc_base: str) -> str:
    chain = [elem]
    parent = parent_map.get(elem)
    while parent is not None:
        chain.append(parent)
        parent = parent_map.get(parent)
    base = doc_base
    for node in reversed(chain):
        value = node.get(f"{{{XML_NS}}}base")
        if value:
            base = _join(base, value)
    return base


def read_catalog_rules(archive: zipfile.ZipFile, top: str) -> list[tuple[str, str]]:
    root = ET.fromstring(archive.read(f"{top}/META-INF/catalog.xml"))
    rules = []
    for elem in root.iter():
        if elem.tag == f"{{{CAT_NS}}}rewriteURI":
            start = elem.attrib["uriStartString"]
            prefix = elem.attrib["rewritePrefix"]
            # Relative rewrite prefixes resolve against META-INF/.
            if not prefix.startswith("xbrl-pkg:"):
                prefix = "xbrl-pkg://package" + _norm(
                    "/META-INF/" + prefix)
            rules.append((start, prefix))
    return sorted(rules, key=lambda r: len(r[0]), reverse=True)


def rewrite(uri: str, rules) -> str:
    """Longest matching prefix; boundary slash collapsed."""
    for start, prefix in rules:
        if uri.startswith(start):
            suffix = uri[len(start):]
            if prefix.endswith("/") and suffix.startswith("/"):
                suffix = suffix[1:]
            return prefix + suffix
    raise AssertionError(f"no catalog rule matches {uri}")


def to_member(logical_uri: str) -> str:
    parts = urlsplit(logical_uri)
    assert parts.scheme == "xbrl-pkg"
    # urlsplit().path begins with "/"; the package member path is relative.
    return _norm(unquote(parts.path).lstrip("/"))


def _parent_map(root: ET.Element) -> dict:
    return {child: parent for parent in root.iter() for child in list(parent)}


def scan_refs(archive: zipfile.ZipFile, top: str, member_path: str,
              rules) -> list[tuple[str, str, str]]:
    """Return (kind, raw_href, absolute_target_uri) for one XML member."""
    doc_uri = PKG + member_path
    root = ET.fromstring(archive.read(f"{top}/{member_path}"))
    parent_map = _parent_map(root)
    refs: list[tuple[str, str, str]] = []
    for elem in root.iter():
        tag = elem.tag
        if tag == f"{{{XSD_NS}}}import":
            href = elem.attrib.get("schemaLocation")
            if href is None:
                raise AssertionError("import without schemaLocation")
            base = _fold_xml_base(elem, parent_map, doc_uri)
            refs.append(("import", href, _resolve(base, href, rules)))
        elif tag in (f"{{{XSD_NS}}}include", f"{{{XSD_NS}}}redefine"):
            href = elem.attrib["schemaLocation"]
            base = _fold_xml_base(elem, parent_map, doc_uri)
            refs.append((tag.split("}", 1)[1], href,
                         _resolve(base, href, rules)))
        elif tag == f"{{{LINK_NS}}}linkbaseRef":
            href = elem.attrib[f"{{{XLINK_NS}}}href"]
            base = _fold_xml_base(elem, parent_map, doc_uri)
            refs.append(("linkbaseRef", href,
                         _resolve(base, href, rules)))
    return refs


def _resolve(base: str, href: str, rules) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return rewrite(href.split("#", 1)[0], rules)
    return _join(base, href)


def expected_closure(zip_path: str) -> dict:
    """Independently walk the closure from the two declared entry points."""
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        top = names[0].split("/", 1)[0]
        rules = read_catalog_rules(archive, top)

        desc_root = ET.fromstring(
            archive.read(f"{top}/META-INF/taxonomyPackage.xml"))
        entries = []
        for doc in desc_root.iter():
            if doc.tag.endswith("}entryPointDocument"):
                href = doc.attrib["href"]
                entries.append(PKG + _norm(href))

        # Iterative DFS, three-colour, exactly like the production resolver
        # but written independently here.
        state: dict[str, int] = {}
        files: dict[str, str] = {}
        edges: list[tuple[str, str, str, bool]] = []
        stack = [(uri, 0) for uri in sorted(entries)]
        for uri in entries:
            state[uri] = 1
            files[uri] = to_member(uri)
        refs_cache: dict[str, list] = {}

        def refs_of(uri: str):
            if uri not in refs_cache:
                refs_cache[uri] = scan_refs(
                    archive, top, files[uri], rules)
            return refs_cache[uri]

        while stack:
            source, index = stack[-1]
            refs = refs_of(source)
            if index >= len(refs):
                state[source] = 2
                stack.pop()
                continue
            stack[-1] = (source, index + 1)
            kind, raw, target = refs[index]
            is_cycle = state.get(target) == 1
            edge = (source, target, kind, is_cycle)
            if edge not in edges:
                edges.append(edge)
            if state.get(target) in (1, 2):
                continue
            state[target] = 1
            files[target] = to_member(target)
            stack.append((target, 0))

    return {
        "entries": tuple(sorted(entries)),
        "files": dict(sorted(files.items())),
        "edges": tuple(sorted(edges)),
        "rules": tuple(rules),
    }
