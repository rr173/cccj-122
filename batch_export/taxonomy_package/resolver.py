"""Dependency-closure resolver for entry schemas inside a registered package.

Given a package's catalog rules and members, the resolver walks the XSD
``import``/``include``/``redefine`` references and XBRL ``linkbaseRef``
references reachable from one or more entry schemas. Every reference is first
turned into an absolute URI using the document's logical URI and its folded
``xml:base``; only then is the catalog consulted (longest matching
``uriStartString`` wins). Public URIs without a matching rule are refused,
never fetched. The traversal is an explicit-stack depth-first search with
three-colour marking, so dependency cycles are recorded rather than followed
forever and the output is independent of visit order.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import (
    PackageFormatError,
    ResolutionError,
    SourceLocation,
    located,
)
from .xmlutil import XmlSecurityError
from .models import (
    XBRL_LINK_NS,
    XLINK_NS,
    XSD_NS,
    CatalogRule,
    PackageMember,
    ReferenceRecord,
    ResolutionEdge,
    ResolutionFile,
)
from .uriutil import (
    apply_rewrite,
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
)

WHITE, GRAY, BLACK = 0, 1, 2

_SCHEMA_REF_LOCALS = frozenset({"import", "include", "redefine"})


@dataclass
class _Node:
    public_uri: str
    member_path: str
    is_entry: bool
    references: list[ReferenceRecord] = field(default_factory=list)
    is_xml: bool = True


class SchemaResolver:
    """Resolve entry schemas against one already-validated package."""

    def __init__(self, *, package_sha256: str,
                 members: tuple[PackageMember, ...],
                 catalog_rules: tuple[CatalogRule, ...],
                 payloads: dict[str, bytes]):
        self.package_sha256 = package_sha256
        self._members = {m.path: m for m in members}
        self._rules = catalog_rules
        self._payloads = payloads
        # Longest-prefix index, built once. Equal-length rules with different
        # targets were rejected while parsing the catalog; defensively keep
        # the deterministic check here as well.
        self._rules_by_prefix = sorted(
            catalog_rules, key=lambda r: len(r.uri_start_string), reverse=True)

    # -- public API -----------------------------------------------------------

    def resolve(self, entry_uris: tuple[str, ...]) -> tuple[
            tuple[ResolutionFile, ...], tuple[ResolutionEdge, ...]]:
        """Resolve the full closure of the given entry logical URIs."""
        # Deterministic entry order; the closure itself is order-independent.
        entries = tuple(sorted(set(entry_uris)))
        states: dict[str, int] = {}
        nodes: dict[str, _Node] = {}
        edges: list[ResolutionEdge] = []
        edge_index: set[tuple[str, str, str]] = set()

        for entry_uri in entries:
            node = self._materialize(entry_uri, is_entry=True)
            if entry_uri not in nodes:
                nodes[entry_uri] = node
            else:
                # Same entry listed twice stays one node, but keep the flag.
                nodes[entry_uri].is_entry = True
            states.setdefault(entry_uri, WHITE)

        # Iterative DFS. Each stack frame is (uri, next-reference-index).
        for root in entries:
            if states.get(root) != WHITE:
                continue
            states[root] = GRAY
            stack: list[tuple[str, int]] = [(root, 0)]
            while stack:
                source_uri, index = stack[-1]
                references = nodes[source_uri].references
                if index >= len(references):
                    states[source_uri] = BLACK
                    stack.pop()
                    continue
                stack[-1] = (source_uri, index + 1)
                ref = references[index]
                target_uri = self._absolute_reference(source_uri, ref)
                edge_key = (source_uri, target_uri, ref.kind)
                if edge_key not in edge_index:
                    edge_index.add(edge_key)
                    is_cycle = states.get(target_uri) == GRAY
                    edges.append(ResolutionEdge(
                        source_uri=source_uri,
                        target_uri=target_uri,
                        kind=ref.kind,
                        raw_href=ref.raw_href,
                        is_cycle=is_cycle,
                    ))
                if states.get(target_uri) == GRAY:
                    continue  # back edge: the GRAY node is already on the stack
                if states.get(target_uri) == BLACK:
                    continue  # shared dependency already fully explored
                target_node = self._materialize(target_uri, is_entry=False)
                nodes[target_uri] = target_node
                states[target_uri] = GRAY
                stack.append((target_uri, 0))

        files = tuple(sorted(
            (ResolutionFile(
                public_uri=uri,
                member_path=node.member_path,
                member_sha256=self._members[node.member_path].sha256,
                byte_size=self._members[node.member_path].byte_size,
                is_entry=node.is_entry,
                is_xml=node.is_xml,
            ) for uri, node in nodes.items()),
            key=lambda f: f.public_uri))
        stable_edges = tuple(sorted(
            edges, key=lambda e: (e.source_uri, e.target_uri, e.kind)))
        return files, stable_edges

    # -- URI to member mapping ------------------------------------------------

    def _absolute_reference(self, source_uri: str,
                            ref: ReferenceRecord) -> str:
        """Fold xml:base, then resolve the reference to an absolute URI."""
        try:
            absolute = strip_fragment(join_uri(ref.effective_base, ref.raw_href))
        except ValueError as exc:
            raise ResolutionError(located(
                f"reference {ref.raw_href!r} is not a valid URI: {exc}",
                SourceLocation(source_uri, ref.line))) from exc
        return self._map_uri(absolute, source_uri, ref)

    def _map_uri(self, absolute: str, source_uri: str,
                 ref: ReferenceRecord) -> str:
        """Catalog lookup (longest prefix) and containment validation."""
        location = SourceLocation(source_uri, ref.line)
        if is_logical_uri(absolute):
            logical = absolute
        else:
            rule = self._longest_match(absolute)
            if rule is None:
                raise ResolutionError(located(
                    f"public URI {absolute!r} (from {ref.kind} href"
                    f" {ref.raw_href!r}) has no matching catalog rewriteURI"
                    " rule; network lookups are not performed", location))
            logical = apply_rewrite(absolute, rule.uri_start_string,
                                    rule.rewrite_prefix)
        member_path = self._to_member(logical, location)
        if member_path not in self._members:
            raise ResolutionError(located(
                f"reference target {absolute!r} maps to package member"
                f" {member_path!r}, which does not exist in package"
                f" {self.package_sha256[:12]}", location))
        return logical

    def _longest_match(self, uri: str):
        """Return the longest-prefix matching rule, or None.

        Rules are indexed longest-first. Two rules can only make one URI
        ambiguous if they share an identical ``uriStartString`` with
        different targets; that case is rejected when the catalog is parsed,
        so here the first (longest) match is always unambiguous.
        """
        for rule in self._rules_by_prefix:
            if uri.startswith(rule.uri_start_string):
                return rule
        return None

    def _to_member(self, logical_uri: str,
                   location: SourceLocation) -> str:
        try:
            return logical_uri_to_member_path(
                logical_uri, where=str(location))
        except PackageFormatError as exc:
            raise ResolutionError(located(
                f"target {logical_uri!r} escapes the package after URI"
                f" decoding/normalisation: {exc}", location)) from exc

    # -- member materialisation and scanning ----------------------------------

    def _materialize(self, logical_uri: str, *, is_entry: bool) -> _Node:
        member_path = self._to_member(
            logical_uri, SourceLocation(logical_uri, 0))
        member = self._members.get(member_path)
        if member is None:
            raise ResolutionError(
                f"entry/reference target {logical_uri!r} maps to missing package"
                f" member {member_path!r}")
        node = _Node(public_uri=logical_uri, member_path=member_path,
                     is_entry=is_entry, is_xml=member.is_xml)
        if not member.is_xml:
            # A non-XML member reached through a schema reference cannot be a
            # schema/linkbase document; the reference itself is invalid.
            raise ResolutionError(
                f"{logical_uri}:{0}: referenced member {member_path!r} is not"
                " an XML document")
        data = self._payloads[member_path]
        node.references = self._scan_document(data, logical_uri)
        return node

    def _scan_document(self, data: bytes,
                       document_uri: str) -> list[ReferenceRecord]:
        try:
            root = parse_xml_bytes(data, document_uri)
        except XmlSecurityError as exc:
            raise ResolutionError(
                f"{document_uri}: {exc}") from exc
        except PackageFormatError as exc:
            raise ResolutionError(
                f"{document_uri}: referenced XML is not well-formed: {exc}") \
                from exc

        references: list[ReferenceRecord] = []
        if ns_of(root.tag) == XSD_NS and local_of(root.tag) == "schema":
            for child in child_elements(root):
                if ns_of(child.tag) != XSD_NS:
                    continue
                local = local_of(child.tag)
                if local not in _SCHEMA_REF_LOCALS:
                    continue
                if local == "import" and child.get("schemaLocation") is None:
                    raise ResolutionError(
                        f"{document_uri}:{child.sourceline}: xs:import without a"
                        " schemaLocation attribute is outside the supported"
                        " subset; every import must name a resolvable schema")
                href = child.get("schemaLocation")
                if href is None:
                    # include/redefine always require it per XML Schema;
                    # lxml cannot validate that for us here.
                    raise ResolutionError(
                        f"{document_uri}:{child.sourceline}: xs:{local} without a"
                        " schemaLocation attribute cannot be resolved")
                references.append(self._record(local, href, child, document_uri))

        # linkbaseRef may appear inside an XSD (appinfo) or in a linkbase
        # document; scan the whole tree regardless of the root namespace.
        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            if is_qname(elem, XBRL_LINK_NS, "linkbaseRef"):
                href = elem.get(f"{{{XLINK_NS}}}href")
                if not href:
                    raise ResolutionError(
                        f"{document_uri}:{elem.sourceline}: link:linkbaseRef"
                        " without an xlink:href attribute cannot be resolved")
                references.append(
                    self._record("linkbaseRef", href, elem, document_uri))

        references.sort(key=lambda r: (r.line, r.kind, r.raw_href))
        return references

    @staticmethod
    def _record(kind: str, href: str, elem, document_uri: str) -> ReferenceRecord:
        return ReferenceRecord(
            kind=kind,
            raw_href=href,
            effective_base=effective_base(elem, document_uri),
            line=int(elem.sourceline or 0),
        )
