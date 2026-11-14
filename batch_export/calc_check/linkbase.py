"""Local calculation linkbase reader and explicit locator mapping loader.

Only three element kinds are understood, exactly as the task scopes them:

* ``link:calculationLink`` extended links (one role each);
* ``link:loc`` locators whose ``xlink:href`` must be a same-document
  ``#fragment`` reference;
* ``link:calculationArc`` arcs whose ``xlink:arcrole`` must be the standard
  summation-item arcrole.

Every ``xlink`` attribute is checked by its semantics (``type``/``role``/
``label``/``href``/``from``/``to``/``arcrole``). Labels are resolved inside the
extended link that declares them, so the same label text in another link never
leaks across. The parser only records structure; concept resolution against the
locator mapping and the concept dictionary happens in
:mod:`calc_check.network`, where locator locations can be attached to errors.
"""
from __future__ import annotations

import json
import xml.parsers.expat as expat
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from .errors import (
    LINK_NS,
    SUMMATION_ITEM_ARCROLE,
    XLINK_NS,
    LinkbaseError,
    SourceLocation,
)

_NS_SEP = "\x01"
_DECIMAL_LEXICAL_RE = __import__("re").compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
_NONNEG_INT_RE = __import__("re").compile(r"^\d+$")


@dataclass(frozen=True)
class LocRecord:
    """One ``link:loc`` inside one extended link."""

    label: str
    href: str
    fragment: str
    location: SourceLocation


@dataclass(frozen=True)
class ArcRecord:
    """One ``link:calculationArc`` as written, before priority resolution."""

    role: str
    from_label: str
    to_label: str
    weight: Decimal
    priority: int
    use: str  # "optional" | "prohibited"
    location: SourceLocation


@dataclass(frozen=True)
class ExtendedLink:
    role: str
    location: SourceLocation
    locs: tuple[LocRecord, ...]
    arcs: tuple[ArcRecord, ...]


@dataclass(frozen=True)
class ParsedLinkbase:
    path: str
    links: tuple[ExtendedLink, ...]


# ---------------------------------------------------------------------------
# Locator mapping
# ---------------------------------------------------------------------------

def load_locator_mapping(source: str | Path | dict | list) -> dict[str, str]:
    """Load the explicit ``#fragment -> expanded concept`` mapping table.

    Accepted JSON shapes::

        {"mappings": [{"fragment": "#rev", "concept": "{ns}Revenue"}, ...]}

        [{"fragment": "#rev", "concept": "{ns}Revenue"}, ...]

        {"#rev": "{ns}Revenue", ...}

    The mapping is explicit: a fragment missing from it can never be resolved by
    fetching a taxonomy, and duplicate fragments or malformed expanded names
    reject the whole definition.
    """
    if isinstance(source, (dict, list)):
        payload = source
        origin = "<mapping argument>"
    else:
        path = Path(source)
        origin = str(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LinkbaseError(f"locator mapping file not found: {path}") from None
        except (OSError, ValueError) as exc:
            raise LinkbaseError(f"cannot read locator mapping {path}: {exc}") from None
    if isinstance(payload, dict) and "mappings" in payload:
        payload = payload["mappings"]
    entries: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        entries = list(payload.items())
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or "fragment" not in item or "concept" not in item:
                raise LinkbaseError(
                    f"{origin}: each mapping entry needs 'fragment' and 'concept' keys"
                )
            entries.append((str(item["fragment"]), str(item["concept"])))
    else:
        raise LinkbaseError(f"{origin}: mapping must be a JSON object or array")
    if not entries:
        raise LinkbaseError(f"{origin}: locator mapping is empty")
    result: dict[str, str] = {}
    for fragment, concept in entries:
        if not fragment.startswith("#") or len(fragment) < 2:
            raise LinkbaseError(
                f"{origin}: mapping fragment must look like '#id', got {fragment!r}"
            )
        if not concept.startswith("{") or "}" not in concept or concept.endswith("}"):
            raise LinkbaseError(
                f"{origin}: concept for {fragment!r} must be an expanded QName "
                f"{{namespaceURI}}localName, got {concept!r}"
            )
        namespace, local = concept[1:].split("}", 1)
        if not namespace or not local:
            raise LinkbaseError(
                f"{origin}: concept for {fragment!r} has an empty namespace or local name"
            )
        if fragment in result:
            raise LinkbaseError(
                f"{origin}: fragment {fragment!r} is mapped more than once"
            )
        result[fragment] = concept
    return result


# ---------------------------------------------------------------------------
# Linkbase XML parser
# ---------------------------------------------------------------------------

class _LinkbaseParser:
    def __init__(self, path: str):
        self.path = path
        self._links: list[ExtendedLink] = []
        self._locs: list[LocRecord] = []
        self._arcs: list[ArcRecord] = []
        self._labels: dict[str, LocRecord] = {}
        self._stack: list[tuple[str, str]] = []
        self._current_role: Optional[str] = None
        self._current_link_loc: Optional[SourceLocation] = None
        self._parser: Optional[expat.XMLParserType] = None

    def parse(self, content: bytes) -> ParsedLinkbase:
        parser = expat.ParserCreate(namespace_separator=_NS_SEP)
        parser.StartElementHandler = self._on_start
        parser.EndElementHandler = self._on_end
        parser.CharacterDataHandler = self._on_text
        parser.buffer_text = True
        self._parser = parser
        try:
            parser.Parse(content, True)
        except expat.ExpatError as exc:
            raise LinkbaseError(
                f"not well-formed XML: {expat.ErrorString(exc.code)}",
                SourceLocation(self.path, exc.lineno, exc.offset + 1),
            ) from None
        if not self._links:
            raise LinkbaseError(f"{self.path}: linkbase contains no calculationLink")
        return ParsedLinkbase(path=self.path, links=tuple(self._links))

    # -- helpers --------------------------------------------------------------

    def _loc(self) -> SourceLocation:
        return SourceLocation(
            self.path,
            self._parser.CurrentLineNumber,
            self._parser.CurrentColumnNumber + 1,
        )

    def _attr(self, attrs: dict, local: str, namespace: str = XLINK_NS) -> Optional[str]:
        return attrs.get(f"{namespace}{_NS_SEP}{local}")

    def _require_attr(self, attrs: dict, local: str, element: str, loc: SourceLocation) -> str:
        value = self._attr(attrs, local)
        if value is None or value == "":
            raise LinkbaseError(
                f"link:{element} requires a non-empty xlink:{local} attribute", loc
            )
        return value

    def _reject_unknown_attrs(self, attrs: dict, allowed: set[str], element: str,
                              loc: SourceLocation) -> None:
        for name in attrs:
            if name not in allowed:
                uri, local = name.split(_NS_SEP, 1)
                shown = local if uri == XLINK_NS else f"{{{uri}}}{local}"
                raise LinkbaseError(
                    f"link:{element}: unsupported attribute {shown!r}", loc
                )

    # -- expat handlers --------------------------------------------------------

    def _on_text(self, data: str) -> None:
        if data.strip():
            raise LinkbaseError(
                "calculation linkbase elements carry no text content", self._loc()
            )

    def _on_start(self, name: str, attrs: dict) -> None:
        uri, local = name.split(_NS_SEP, 1) if _NS_SEP in name else (None, name)
        loc = self._loc()
        depth = len(self._stack)
        if depth == 0:
            if uri != LINK_NS or local != "linkbase":
                raise LinkbaseError(
                    "root element must be {http://www.xbrl.org/2003/linkbase}linkbase",
                    loc,
                )
            self._reject_unknown_attrs(attrs, set(), "linkbase", loc)
            self._stack.append((uri or "", local))
            return
        parent_uri, parent_local = self._stack[-1]
        if parent_local == "linkbase":
            if uri != LINK_NS or local != "calculationLink":
                raise LinkbaseError(
                    f"unsupported structure {{{uri}}}{local}: only link:calculationLink "
                    "is accepted directly under link:linkbase",
                    loc,
                )
            self._start_calculation_link(attrs, loc)
        elif parent_local == "calculationLink":
            if uri != LINK_NS or local not in ("loc", "calculationArc"):
                raise LinkbaseError(
                    f"unsupported structure {{{uri}}}{local} inside calculationLink: "
                    "only link:loc and link:calculationArc are accepted",
                    loc,
                )
            if local == "loc":
                self._start_loc(attrs, loc)
            else:
                self._start_arc(attrs, loc)
        else:
            raise LinkbaseError(
                f"unsupported nested structure {{{uri}}}{local} inside link:{parent_local}",
                loc,
            )
        self._stack.append((uri or "", local))

    def _on_end(self, _name: str) -> None:
        uri, local = self._stack.pop()
        if local == "calculationLink":
            if not self._arcs:
                raise LinkbaseError(
                    f"calculationLink role {self._current_role!r} contains no "
                    "calculationArc",
                    self._current_link_loc,
                )
            self._links.append(ExtendedLink(
                role=self._current_role,
                location=self._current_link_loc,
                locs=tuple(self._locs),
                arcs=tuple(self._arcs),
            ))
            self._locs = []
            self._arcs = []
            self._labels = {}
            self._current_role = None
            self._current_link_loc = None

    def _start_calculation_link(self, attrs: dict, loc: SourceLocation) -> None:
        self._reject_unknown_attrs(
            attrs, {f"{XLINK_NS}{_NS_SEP}type", f"{XLINK_NS}{_NS_SEP}role"},
            "calculationLink", loc,
        )
        xlink_type = self._require_attr(attrs, "type", "calculationLink", loc)
        if xlink_type != "extended":
            raise LinkbaseError(
                f"calculationLink xlink:type must be 'extended', got {xlink_type!r}", loc
            )
        role = self._require_attr(attrs, "role", "calculationLink", loc)
        self._current_role = role
        self._current_link_loc = loc

    def _start_loc(self, attrs: dict, loc: SourceLocation) -> None:
        allowed = {
            f"{XLINK_NS}{_NS_SEP}type", f"{XLINK_NS}{_NS_SEP}href",
            f"{XLINK_NS}{_NS_SEP}label",
        }
        self._reject_unknown_attrs(attrs, allowed, "loc", loc)
        xlink_type = self._require_attr(attrs, "type", "loc", loc)
        if xlink_type != "locator":
            raise LinkbaseError(
                f"link:loc xlink:type must be 'locator', got {xlink_type!r}", loc
            )
        label = self._require_attr(attrs, "label", "loc", loc)
        href = self._require_attr(attrs, "href", "loc", loc)
        if not href.startswith("#") or len(href) < 2 or "/" in href or "?" in href:
            raise LinkbaseError(
                f"link:loc href must be a same-document '#fragment' reference, "
                f"got {href!r}",
                loc,
            )
        fragment = href[1:]
        if label in self._labels:
            first = self._labels[label].location
            raise LinkbaseError(
                f"duplicate xlink:label {label!r} inside this calculationLink; "
                f"first declared at {first}",
                loc,
            )
        record = LocRecord(label=label, href=href, fragment=fragment, location=loc)
        self._labels[label] = record
        self._locs.append(record)

    def _start_arc(self, attrs: dict, loc: SourceLocation) -> None:
        allowed = {
            f"{XLINK_NS}{_NS_SEP}type", f"{XLINK_NS}{_NS_SEP}from",
            f"{XLINK_NS}{_NS_SEP}to", f"{XLINK_NS}{_NS_SEP}arcrole",
            "weight", "priority", "use",
        }
        self._reject_unknown_attrs(attrs, allowed, "calculationArc", loc)
        xlink_type = self._require_attr(attrs, "type", "calculationArc", loc)
        if xlink_type != "arc":
            raise LinkbaseError(
                f"calculationArc xlink:type must be 'arc', got {xlink_type!r}", loc
            )
        arcrole = self._require_attr(attrs, "arcrole", "calculationArc", loc)
        if arcrole != SUMMATION_ITEM_ARCROLE:
            raise LinkbaseError(
                f"unsupported arcrole {arcrole!r}; only the summation-item arcrole "
                f"{SUMMATION_ITEM_ARCROLE} is accepted",
                loc,
            )
        from_label = self._require_attr(attrs, "from", "calculationArc", loc)
        to_label = self._require_attr(attrs, "to", "calculationArc", loc)
        weight_text = attrs.get("weight")
        if weight_text is None or not _DECIMAL_LEXICAL_RE.match(weight_text.strip()):
            raise LinkbaseError(
                f"calculationArc weight must be an exact decimal, got "
                f"{weight_text!r}",
                loc,
            )
        try:
            weight = Decimal(weight_text.strip())
        except InvalidOperation:
            raise LinkbaseError(f"invalid arc weight {weight_text!r}", loc) from None
        if not weight.is_finite() or weight == 0:
            raise LinkbaseError(
                f"calculationArc weight must be a finite non-zero decimal, got "
                f"{weight_text!r}",
                loc,
            )
        priority_text = attrs.get("priority")
        if priority_text is None:
            priority = 0
        elif not _NONNEG_INT_RE.match(priority_text.strip()):
            raise LinkbaseError(
                f"calculationArc priority must be a non-negative integer, got "
                f"{priority_text!r}",
                loc,
            )
        else:
            priority = int(priority_text.strip())
        use = attrs.get("use")
        if use is None:
            use = "optional"
        elif use not in ("optional", "prohibited"):
            raise LinkbaseError(
                f"calculationArc use must be 'optional' or 'prohibited', got {use!r}",
                loc,
            )
        self._arcs.append(ArcRecord(
            role=self._current_role,
            from_label=from_label,
            to_label=to_label,
            weight=weight,
            priority=priority,
            use=use,
            location=loc,
        ))


def parse_linkbase_file(path: str | Path) -> ParsedLinkbase:
    """Read and structurally validate a local calculation linkbase XML file."""
    path = str(path)
    try:
        content = Path(path).read_bytes()
    except FileNotFoundError:
        raise LinkbaseError(f"calculation linkbase not found: {path}") from None
    except OSError as exc:
        raise LinkbaseError(f"cannot read calculation linkbase {path}: {exc}") from None
    return _LinkbaseParser(path).parse(content)
