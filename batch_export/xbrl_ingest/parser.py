"""Streaming, namespace-aware XBRL 2.1 instance reader.

Only the supported subset is read: contexts (entity + period + explicitMember
dimensions), units (measure products and divide), and facts. Footnote links,
tuples, typed members and any other element inside the instance are refused.
Facts are queued as they appear; context/unit references are resolved only after
the whole document has been parsed, so a fact may precede the target it
references.

Nothing here touches SQLite; parsing produces an in-memory ParsedDocument that
the ingester commits atomically or discards entirely.
"""
from __future__ import annotations

import xml.parsers.expat as expat
from dataclasses import dataclass, field
from typing import Optional

from .errors import (
    ConceptSpec,
    DuplicateIdError,
    InvalidValueError,
    MissingReferenceError,
    ParsedDecimal,
    ParsedFact,
    SourceLocation,
    SUPPORTED_XBRLI_LOCALS,
    UnknownConceptError,
    XBRLDI_NS,
    XBRLI_NS,
    XbrlStructureError,
    XbrlXmlError,
    XSI_NS,
    XML_NS,
)
from .models import (
    ContextCoordinate,
    DeclaredContext,
    DeclaredUnit,
    Entity,
    ParsedDocument,
    Period,
    UnitCoordinate,
    UnitMeasureTerm,
)
from .qname import (
    canonical_decimal_text,
    parse_decimal,
    parse_qname_text,
    split_expanded,
    validate_date,
    validate_decimals,
)

_NS_SEP = "\x01"
_ROOT_KIND = "root"


@dataclass
class _Frame:
    kind: str
    uri: Optional[str]
    local: str
    attrs: dict
    location: SourceLocation
    scope: dict          # prefix -> uri* in force *inside* this element
    default_ns: Optional[str]
    lang: Optional[str]
    text: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    children_data: list = field(default_factory=list)


class InstanceParser:
    """Parses bytes into a ParsedDocument; any problem raises IngestError."""

    def __init__(self, path: str, concepts: dict[tuple[str, str], ConceptSpec]):
        self.path = path
        self._concepts = concepts
        self._stack: list[_Frame] = []
        self._pending_ns: dict[str, Optional[str]] = {}
        self._contexts: dict[str, DeclaredContext] = {}
        self._units: dict[str, DeclaredUnit] = {}
        self._drafts: list[dict] = []
        self._fact_position = 0
        self._root_loc: Optional[SourceLocation] = None

    # -- public ---------------------------------------------------------------

    def parse(self, content: bytes) -> ParsedDocument:
        parser = expat.ParserCreate(namespace_separator=_NS_SEP)
        parser.StartNamespaceDeclHandler = self._on_start_ns
        parser.EndNamespaceDeclHandler = self._on_end_ns
        parser.StartElementHandler = self._on_start
        parser.EndElementHandler = self._on_end
        parser.CharacterDataHandler = self._on_text
        parser.CommentHandler = lambda _data: None
        parser.ProcessingInstructionHandler = lambda *_: None
        parser.ExternalEntityRefHandler = lambda *_: 0
        parser.buffer_text = True
        self._parser = parser
        try:
            parser.Parse(content, True)
        except expat.ExpatError as exc:
            raise XbrlXmlError(
                f"not well-formed XML: {expat.ErrorString(exc.code)}",
                SourceLocation(self.path, exc.lineno, exc.offset + 1),
            ) from None
        if self._root_loc is None:
            raise XbrlStructureError(f"{self.path}: no XBRL instance element found")
        if self._stack:
            raise XbrlStructureError(f"{self.path}: document ended inside an element")
        _check_reference_targets(self._contexts, self._units, self._drafts, self.path)
        document = ParsedDocument(
            path=self.path,
            content=content,
            content_sha256="",
            contexts=list(self._contexts.values()),
            units=list(self._units.values()),
        )
        document.facts = [draft["fact"] for draft in self._drafts]
        document.document_element = "xbrl"
        return document

    # -- namespace tracking ---------------------------------------------------

    def _on_start_ns(self, prefix: Optional[str], uri: Optional[str]) -> None:
        # expat reports declarations immediately before the start tag that they
        # are written on, so they belong to the element parsed next.
        self._pending_ns[prefix or ""] = uri

    def _on_end_ns(self, _prefix: Optional[str]) -> None:
        pass  # scope lives in the frame stack; no action needed on close

    def _scope_for_next(self) -> tuple[dict, Optional[str]]:
        parent = self._stack[-1] if self._stack else None
        prefixes = dict(parent.scope) if parent else {"xml": XML_NS}
        default_ns = parent.default_ns if parent else None
        for prefix, uri in self._pending_ns.items():
            if prefix == "":
                default_ns = uri  # "" URI means an explicit undeclaration
            else:
                prefixes[prefix] = uri
        self._pending_ns = {}
        return prefixes, default_ns

    # -- element handlers -----------------------------------------------------

    def _on_start(self, name: str, attrs: dict) -> None:
        uri, local = split_expanded(name)
        location = SourceLocation(
            self.path, self._parser.CurrentLineNumber, self._parser.CurrentColumnNumber + 1
        )
        prefixes, default_ns = self._scope_for_next()
        parent = self._stack[-1] if self._stack else None
        own_lang = _xml_attr(attrs, "lang")
        lang = own_lang if own_lang is not None else (parent.lang if parent else None)
        if lang == "":
            lang = None
        frame = _Frame(
            kind="?",
            uri=uri,
            local=local,
            attrs=attrs,
            location=location,
            scope=prefixes,
            default_ns=default_ns,
            lang=lang,
        )
        if parent is None:
            self._start_root(frame)
        else:
            self._start_child(frame, parent)
        self._stack.append(frame)

    def _start_root(self, frame: _Frame) -> None:
        self._root_loc = frame.location
        if frame.uri != XBRLI_NS or frame.local != "xbrl":
            raise XbrlStructureError(
                f"root element must be {{{XBRLI_NS}}}xbrl, got "
                f"{{{frame.uri}}}{frame.local}" if frame.uri else
                f"root element must be {{{XBRLI_NS}}}xbrl, got <{frame.local}>",
                frame.location,
            )
        frame.kind = _ROOT_KIND

    def _start_child(self, frame: _Frame, parent: _Frame) -> None:
        pkind = parent.kind
        if pkind == "fact":
            parent_concept = f"{{{parent.uri}}}{parent.local}"
            raise XbrlStructureError(
                f"nested content inside fact {parent_concept}; tuples, footnotes "
                "and nested facts are out of scope",
                frame.location,
            )
        if pkind in _SIMPLE_KINDS:
            raise XbrlStructureError(
                f"<{parent.local}> must contain text only",
                frame.location,
            )
        if frame.uri is None:
            raise XbrlStructureError(
                f"element <{frame.local}> is in no namespace; "
                "the instance must use XBRL 2.1 namespaces",
                frame.location,
            )
        if pkind in ("segment", "scenario"):
            if frame.uri != XBRLDI_NS:
                raise XbrlStructureError(
                    f"unsupported dimension content {{{frame.uri}}}{frame.local} inside "
                    f"xbrli:{pkind}; only xbrldi:explicitMember is supported",
                    frame.location,
                )
            if frame.local == "explicitMember":
                frame.kind = "explicitMember"
            elif frame.local == "typedMember":
                raise XbrlStructureError(
                    "typedMember is out of scope; only explicitMember dimensions "
                    "are supported",
                    frame.location,
                )
            else:
                raise XbrlStructureError(
                    f"unsupported element <{frame.local}> inside xbrli:{pkind}; "
                    "only explicitMember dimensions are supported",
                    frame.location,
                )
            return
        if frame.uri != XBRLI_NS:
            # A concept fact is only allowed directly under xbrli:xbrl.
            if pkind != _ROOT_KIND:
                raise XbrlStructureError(
                    f"unsupported element {{{frame.uri}}}{frame.local} at this position",
                    frame.location,
                )
            self._start_fact(frame)
            return
        local = frame.local
        if local not in SUPPORTED_XBRLI_LOCALS or local == "xbrl":
            raise XbrlStructureError(
                f"unsupported XBRL 2.1 structure {{{frame.uri}}}{local}"
                + (" (tuples/footnotes are out of scope)" if local in ("tuple", "footnoteLink") else ""),
                frame.location,
            )
        if pkind == _ROOT_KIND:
            if local == "context":
                self._start_context(frame)
            elif local == "unit":
                self._start_unit(frame)
            else:
                raise XbrlStructureError(
                    f"unexpected <{local}> directly under xbrli:xbrl",
                    frame.location,
                )
        elif pkind == "context":
            if local in ("entity", "period", "scenario"):
                _guard_once(parent, local, frame)
                frame.kind = local
            else:
                raise XbrlStructureError(
                    f"unsupported element <{local}> inside xbrli:context",
                    frame.location,
                )
        elif pkind == "entity":
            if local == "identifier":
                _guard_once(parent, local, frame)
                frame.kind = "identifier"
            elif local == "segment":
                _guard_once(parent, local, frame)
                frame.kind = "segment"
            else:
                raise XbrlStructureError(
                    f"unsupported element <{local}> inside xbrli:entity",
                    frame.location,
                )
        elif pkind == "period":
            if local in ("instant", "startDate", "endDate"):
                if local in parent.data.get("seen", set()):
                    raise XbrlStructureError(
                        f"period has more than one <{local}>", frame.location
                    )
                parent.data.setdefault("seen", set()).add(local)
                frame.kind = local
            else:
                raise XbrlStructureError(
                    f"unsupported element <{local}> inside xbrli:period",
                    frame.location,
                )
        elif pkind == "unit":
            if local == "measure":
                frame.kind = "measure"
            elif local == "divide":
                _guard_once(parent, "divide", frame)
                frame.kind = "divide"
            else:
                raise XbrlStructureError(
                    f"unsupported element <{local}> inside xbrli:unit", frame.location
                )
        elif pkind == "divide":
            if local == "unitNumerator":
                _guard_once(parent, "unitNumerator", frame)
                frame.kind = "unitNumerator"
            elif local == "unitDenominator":
                _guard_once(parent, "unitDenominator", frame)
                frame.kind = "unitDenominator"
            else:
                raise XbrlStructureError(
                    f"unsupported element <{local}> inside xbrli:divide", frame.location
                )
        elif pkind in ("unitNumerator", "unitDenominator"):
            if local == "measure":
                frame.kind = "measure"
            else:
                raise XbrlStructureError(
                    f"unsupported element <{local}> inside xbrli:{pkind}",
                    frame.location,
                )
        else:  # pragma: no cover - all structural kinds handled above
            raise XbrlStructureError(
                f"unsupported element <{local}> at this position", frame.location
            )

    def _on_end(self, _name: str) -> None:
        frame = self._stack.pop()
        kind = frame.kind
        if kind == _ROOT_KIND:
            return
        parent = self._stack[-1]
        text = "".join(frame.text)
        if kind == "fact":
            self._finish_fact(frame, text)
        elif kind == "identifier":
            self._finish_identifier(frame, parent, text)
        elif kind == "instant":
            self._finish_date(frame, parent, text, "instant")
        elif kind == "startDate":
            self._finish_date(frame, parent, text, "startDate")
        elif kind == "endDate":
            self._finish_date(frame, parent, text, "endDate")
        elif kind == "explicitMember":
            self._finish_explicit_member(frame, parent, text)
        elif kind == "measure":
            self._finish_measure(frame, parent, text)
        elif kind == "context":
            self._finish_context(frame)
        elif kind == "unit":
            self._finish_unit(frame)
        elif kind in ("entity", "period", "segment", "scenario"):
            self._merge_container(frame, parent)
        elif kind in ("unitNumerator", "unitDenominator"):
            key = "numerator" if kind == "unitNumerator" else "denominator"
            parent.data.setdefault(key, []).extend(frame.data.get(key, []))
        elif kind == "divide":
            parent.children_data.append({
                "kind": "divide",
                "numerator": frame.data.get("numerator", []),
                "denominator": frame.data.get("denominator", []),
            })
        else:  # pragma: no cover
            raise AssertionError(f"unhandled frame kind {kind!r}")

    def _merge_container(self, frame: _Frame, parent: _Frame) -> None:
        """Lift entity/period/segment/scenario content into the context frame."""
        data = frame.data
        for key in ("scheme", "identifier", "instant", "startDate", "endDate"):
            if key in data:
                parent.data[key] = data[key]
        if "dimensions" in data:
            target = parent.data.setdefault("dimensions", {})
            locations = parent.data.setdefault("dimension_locations", {})
            for dimension, member in data["dimensions"].items():
                if dimension in target:
                    raise XbrlStructureError(
                        f"duplicate dimension {dimension} in the same context; "
                        f"first declared at {locations[dimension]}",
                        frame.location,
                    )
                target[dimension] = member
                locations[dimension] = data.get("dimension_locations", {}).get(
                    dimension, frame.location
                )

    def _on_text(self, data: str) -> None:
        if self._stack:
            self._stack[-1].text.append(data)

    # -- container starts -----------------------------------------------------

    def _start_context(self, frame: _Frame) -> None:
        frame.kind = "context"
        context_id = frame.attrs.get("id")
        if not context_id:
            raise XbrlStructureError(
                "xbrli:context requires a non-empty id attribute", frame.location
            )
        if context_id in self._contexts:
            first = self._contexts[context_id].location
            raise DuplicateIdError(
                f"duplicate context id {context_id!r}; first declared at {first}",
                frame.location,
            )
        frame.data["id"] = context_id

    def _start_unit(self, frame: _Frame) -> None:
        frame.kind = "unit"
        unit_id = frame.attrs.get("id")
        if not unit_id:
            raise XbrlStructureError(
                "xbrli:unit requires a non-empty id attribute", frame.location
            )
        if unit_id in self._units:
            first = self._units[unit_id].location
            raise DuplicateIdError(
                f"duplicate unit id {unit_id!r}; first declared at {first}",
                frame.location,
            )
        frame.data.update({"id": unit_id, "measures": [], "direct_measures": []})

    def _start_fact(self, frame: _Frame) -> None:
        frame.kind = "fact"
        self._fact_position += 1
        frame.data["position"] = self._fact_position

    # -- simple content finishes ---------------------------------------------

    def _finish_identifier(self, frame: _Frame, parent: _Frame, text: str) -> None:
        scheme = frame.attrs.get("scheme")
        if scheme is None or scheme == "":
            raise XbrlStructureError(
                "xbrli:identifier requires a non-empty scheme attribute",
                frame.location,
            )
        if text == "":
            raise XbrlStructureError(
                "xbrli:identifier must not be empty", frame.location
            )
        parent.data["scheme"] = scheme
        parent.data["identifier"] = text

    def _finish_date(self, frame: _Frame, parent: _Frame, text: str, which: str) -> None:
        value = validate_date(text, frame.location, f"period {which}")
        parent.data[which] = value

    def _finish_explicit_member(self, frame: _Frame, parent: _Frame, text: str) -> None:
        dimension_raw = frame.attrs.get("dimension")
        if not dimension_raw:
            raise XbrlStructureError(
                "xbrli:explicitMember requires a dimension attribute", frame.location
            )
        dim_uri, dim_local = parse_qname_text(
            dimension_raw, frame.scope, frame.default_ns, frame.location,
            what="dimension",
        )
        mem_uri, mem_local = parse_qname_text(
            text, frame.scope, frame.default_ns, frame.location,
            what="explicitMember member",
        )
        dimension = f"{{{dim_uri}}}{dim_local}"
        member = f"{{{mem_uri}}}{mem_local}"
        dims = parent.data.setdefault("dimensions", {})
        if dimension in dims:
            first = parent.data["dimension_locations"][dimension]
            raise XbrlStructureError(
                f"duplicate dimension {dimension} in the same context; "
                f"first declared at {first}",
                frame.location,
            )
        dims[dimension] = member
        parent.data.setdefault("dimension_locations", {})[dimension] = frame.location

    def _finish_measure(self, frame: _Frame, parent: _Frame, text: str) -> None:
        uri, local = parse_qname_text(
            text, frame.scope, frame.default_ns, frame.location, what="unit measure"
        )
        measure = f"{{{uri}}}{local}"
        if parent.kind == "unit":
            parent.data.setdefault("direct_measures", []).append(measure)
        elif parent.kind == "unitNumerator":
            parent.data.setdefault("numerator", []).append(measure)
        elif parent.kind == "unitDenominator":
            parent.data.setdefault("denominator", []).append(measure)
        else:  # pragma: no cover - guarded at start
            raise AssertionError(f"measure inside {parent.kind!r}")

    # -- context / unit completion -------------------------------------------

    def _finish_context(self, frame: _Frame) -> None:
        data = frame.data
        if "scheme" not in data:
            raise XbrlStructureError(
                "context is missing xbrli:entity/xbrli:identifier", frame.location
            )
        period = self._build_period(frame)
        coordinate = ContextCoordinate(
            entity=Entity(scheme=data["scheme"], identifier=data["identifier"]),
            period=period,
            dimensions=tuple(sorted(data.get("dimensions", {}).items())),
        )
        self._contexts[data["id"]] = DeclaredContext(
            context_id=data["id"], coordinate=coordinate, location=frame.location
        )

    def _build_period(self, frame: _Frame) -> Period:
        data = frame.data
        has_instant = "instant" in data
        has_start = "startDate" in data
        has_end = "endDate" in data
        if not (has_instant or has_start or has_end):
            raise XbrlStructureError(
                "context is missing xbrli:period", frame.location
            )
        if has_instant and (has_start or has_end):
            raise XbrlStructureError(
                "period mixes xbrli:instant with xbrli:startDate/endDate",
                frame.location,
            )
        if has_instant:
            return Period(kind="instant", instant=data["instant"])
        if not (has_start and has_end):
            raise XbrlStructureError(
                "duration period requires both xbrli:startDate and xbrli:endDate",
                frame.location,
            )
        try:
            return Period(kind="duration", start=data["startDate"], end=data["endDate"])
        except ValueError as exc:
            raise XbrlStructureError(str(exc), frame.location) from None

    def _finish_unit(self, frame: _Frame) -> None:
        data = frame.data
        direct = data.get("direct_measures", [])
        numerator: list[str] = []
        denominator: list[str] = []
        for child in frame.children_data:
            if child.get("kind") == "divide":
                if direct:
                    raise XbrlStructureError(
                        "unit mixes xbrli:divide with direct xbrli:measure elements",
                        frame.location,
                    )
                numerator = child.get("numerator", [])
                denominator = child.get("denominator", [])
                if not numerator:
                    raise XbrlStructureError(
                        "divide requires a non-empty xbrli:unitNumerator", frame.location
                    )
                if not denominator:
                    raise XbrlStructureError(
                        "divide requires a non-empty xbrli:unitDenominator", frame.location
                    )
        if not numerator and not denominator:
            if not direct:
                raise XbrlStructureError("unit has no xbrli:measure", frame.location)
            numerator = direct
        coordinate = UnitCoordinate(
            numerator=tuple(UnitMeasureTerm(m) for m in numerator),
            denominator=tuple(UnitMeasureTerm(m) for m in denominator),
        )
        self._units[data["id"]] = DeclaredUnit(
            unit_id=data["id"], coordinate=coordinate, location=frame.location
        )

    # -- facts ----------------------------------------------------------------

    def _finish_fact(self, frame: _Frame, text: str) -> None:
        concept = f"{{{frame.uri}}}{frame.local}"
        spec = self._concepts.get((frame.uri, frame.local))
        if spec is None:
            raise UnknownConceptError(
                f"concept {concept} is not in the local concept dictionary",
                frame.location,
            )
        context_ref = frame.attrs.get("contextRef")
        if not context_ref:
            raise XbrlStructureError(
                f"fact {concept} requires a contextRef attribute", frame.location
            )
        unit_ref = frame.attrs.get("unitRef")
        nil_raw = _xml_attr(frame.attrs, "nil", XSI_NS)
        nil = False
        if nil_raw is not None:
            if nil_raw in ("true", "1"):
                nil = True
            elif nil_raw in ("false", "0"):
                nil = False
            else:
                raise InvalidValueError(
                    f"xsi:nil must be true/false/1/0, got {nil_raw!r}", frame.location
                )
        decimals_raw = frame.attrs.get("decimals")
        decimals = validate_decimals(decimals_raw, frame.location) if decimals_raw is not None else None
        value_text: Optional[str] = None
        parsed: Optional[ParsedDecimal] = None
        if spec.type == "decimal":
            if not unit_ref:
                raise XbrlStructureError(
                    f"decimal fact {concept} requires a unitRef attribute",
                    frame.location,
                )
            if not nil and decimals is None:
                raise XbrlStructureError(
                    f"decimal fact {concept} requires a decimals attribute",
                    frame.location,
                )
            if not nil:
                value = parse_decimal(text, frame.location)
                value_text = text
                parsed = ParsedDecimal(
                    value=value,
                    canonical=canonical_decimal_text(value),
                    text=text,
                )
        else:  # text
            if unit_ref is not None:
                raise XbrlStructureError(
                    f"text fact {concept} must not have a unitRef attribute",
                    frame.location,
                )
            if decimals is not None:
                raise XbrlStructureError(
                    f"text fact {concept} must not have a decimals attribute",
                    frame.location,
                )
            if not nil:
                value_text = text
        if nil and text.strip():
            raise InvalidValueError(
                f"xsi:nil fact {concept} must not carry a value", frame.location
            )
        fact = ParsedFact(
            position=frame.data["position"],
            concept=concept,
            concept_type=spec.type,
            context_ref=context_ref,
            unit_ref=unit_ref,
            nil=nil,
            decimals=decimals,
            language=frame.lang if spec.type == "text" else None,
            value_text=value_text,
            value=parsed,
            location=frame.location,
        )
        self._drafts.append({"fact": fact})


_SIMPLE_KINDS = frozenset({
    "identifier", "instant", "startDate", "endDate", "explicitMember", "measure",
})


def _guard_once(parent: _Frame, local: str, frame: _Frame) -> None:
    seen = parent.data.get("_seen_children", set())
    if local in seen:
        raise XbrlStructureError(
            f"<{parent.local}> has more than one <{local}> element", frame.location
        )
    seen.add(local)
    parent.data["_seen_children"] = seen


def _check_reference_targets(contexts, units, drafts, path: str) -> None:
    for draft in drafts:
        fact: ParsedFact = draft["fact"]
        if fact.context_ref not in contexts:
            raise MissingReferenceError(
                f"fact {fact.concept} references undeclared context "
                f"{fact.context_ref!r}",
                fact.location,
            )
        if fact.unit_ref is not None and fact.unit_ref not in units:
            raise MissingReferenceError(
                f"fact {fact.concept} references undeclared unit {fact.unit_ref!r}",
                fact.location,
            )


def _xml_attr(attrs: dict, local: str, namespace: str = XML_NS) -> Optional[str]:
    return attrs.get(f"{namespace}{_NS_SEP}{local}")
