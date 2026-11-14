"""Turn parsed linkbase structure into effective calculation networks.

Resolution rules (a deliberately small, explicit subset of XBRL linkbase
semantics):

* Arcs are grouped by extended-link role: links sharing a role form one network,
  different roles never mix.
* Within a role, arcs are merged by ``(total concept, item concept)``. Only the
  highest ``priority`` layer is considered.
* A prohibited arc on the highest layer cancels the whole relation. Otherwise
  optional arcs on that layer must all carry the same weight; they collapse to
  a single effective edge (a repeated edge never makes the item count twice).
  Conflicting top-layer weights reject the linkbase.
* Lower-priority weights are never added; every original arc keeps its location
  and the reason it was adopted, merged, overridden or prohibited.
* The effective graph must be acyclic; self-loops and long cycles report the
  offending concept path. Prohibited edges take no part in cycle detection.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .errors import LinkbaseError, SourceLocation
from .linkbase import ArcRecord, ExtendedLink, LocRecord, ParsedLinkbase


@dataclass(frozen=True)
class EffectiveEdge:
    role: str
    total: str
    item: str
    weight: Decimal
    locations: tuple[SourceLocation, ...]


@dataclass(frozen=True)
class ArcTrace:
    """The fate of one original calculationArc, kept for the report."""

    role: str
    total: str
    item: str
    weight: Decimal
    priority: int
    use: str
    location: SourceLocation
    disposition: str  # adopted | merged | overridden | prohibited
    reason: str

    def as_text(self) -> dict:
        return {
            "role": self.role,
            "total": self.total,
            "item": self.item,
            "weight": format(self.weight, "f"),
            "priority": self.priority,
            "use": self.use,
            "location": str(self.location),
            "disposition": self.disposition,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ResolvedNetwork:
    role: str
    edges: tuple[EffectiveEdge, ...]
    traces: tuple[ArcTrace, ...]

    @property
    def totals(self) -> tuple[str, ...]:
        return tuple(sorted({edge.total for edge in self.edges}))

    def edges_for_total(self, total: str) -> tuple[EffectiveEdge, ...]:
        return tuple(edge for edge in self.edges if edge.total == total)


@dataclass(frozen=True)
class _ResolvedArc:
    record: ArcRecord
    total: str
    item: str


def _split_concept(concept: str) -> tuple[str, str]:
    namespace, local = concept[1:].split("}", 1)
    return namespace, local


def _resolve_link(
    link: ExtendedLink,
    mapping: dict[str, str],
    concepts: dict[tuple[str, str], object],
) -> list[_ResolvedArc]:
    """Resolve one extended link's labels in its own scope.

    Every locator is validated, even one no arc references: a fragment that
    maps to nothing or to a non-decimal concept must reject the definition
    rather than be silently skipped.
    """
    label_concept: dict[str, str] = {}
    for loc in link.locs:
        # The mapping is keyed by the full same-document reference ('#fragment'),
        # exactly as the locator href is written.
        concept = mapping.get(loc.href)
        if concept is None:
            raise LinkbaseError(
                f"locator {loc.href!r} (label {loc.label!r}) is not present in the "
                "explicit locator mapping; no taxonomy is fetched to resolve it",
                loc.location,
            )
        key = _split_concept(concept)
        spec = concepts.get(key)
        if spec is None:
            raise LinkbaseError(
                f"locator {loc.href!r} maps to concept {concept!r}, which is not "
                "registered in the local concept dictionary",
                loc.location,
            )
        if spec.type != "decimal":
            raise LinkbaseError(
                f"locator {loc.href!r} maps to non-decimal concept {concept!r}; "
                "calculation relationships only support decimal concepts",
                loc.location,
            )
        label_concept[loc.label] = concept

    resolved: list[_ResolvedArc] = []
    for arc in link.arcs:
        total = label_concept.get(arc.from_label)
        if total is None:
            raise LinkbaseError(
                f"calculationArc xlink:from={arc.from_label!r} has no matching link:loc "
                f"label in this calculationLink (role {link.role!r})",
                arc.location,
            )
        item = label_concept.get(arc.to_label)
        if item is None:
            raise LinkbaseError(
                f"calculationArc xlink:to={arc.to_label!r} has no matching link:loc "
                f"label in this calculationLink (role {link.role!r})",
                arc.location,
            )
        resolved.append(_ResolvedArc(record=arc, total=total, item=item))
    return resolved


def _merge_relation(
    role: str,
    total: str,
    item: str,
    members: list[_ResolvedArc],
) -> tuple[Optional[EffectiveEdge], list[ArcTrace]]:
    """Apply the priority/prohibition/dedup rules to one (total, item) group."""
    max_priority = max(member.record.priority for member in members)
    top = [m for m in members if m.record.priority == max_priority]
    lower = [m for m in members if m.record.priority < max_priority]
    traces: list[ArcTrace] = []

    for member in lower:
        rec = member.record
        traces.append(ArcTrace(
            role, total, item, rec.weight, rec.priority, rec.use, rec.location,
            "overridden",
            f"priority {rec.priority} is below the highest priority layer "
            f"{max_priority} for this relation",
        ))

    top_prohibited = [m for m in top if m.record.use == "prohibited"]
    if top_prohibited:
        for member in top:
            rec = member.record
            if rec.use == "prohibited":
                traces.append(ArcTrace(
                    role, total, item, rec.weight, rec.priority, rec.use, rec.location,
                    "prohibited",
                    f"prohibited arc on the highest priority layer {max_priority} "
                    "cancels this relation",
                ))
            else:
                traces.append(ArcTrace(
                    role, total, item, rec.weight, rec.priority, rec.use, rec.location,
                    "overridden",
                    f"optional arc on priority layer {max_priority} is cancelled by a "
                    "prohibited arc on the same layer",
                ))
        return None, traces

    weights = {m.record.weight for m in top}
    if len(weights) > 1:
        detail = "; ".join(
            f"weight {format(m.record.weight, 'f')} at {m.record.location}"
            for m in sorted(top, key=lambda m: (m.record.location.line,
                                                m.record.location.column))
        )
        raise LinkbaseError(
            f"conflicting weights on the highest priority layer ({max_priority}) for "
            f"relation {total} -> {item} in role {role!r}; the linkbase must pick one "
            f"weight: {detail}"
        )
    weight = next(iter(weights))
    ordered = sorted(top, key=lambda m: (m.record.location.line, m.record.location.column))
    first = True
    for member in ordered:
        rec = member.record
        if first:
            traces.append(ArcTrace(
                role, total, item, rec.weight, rec.priority, rec.use, rec.location,
                "adopted",
                f"highest priority layer {max_priority} optional arc adopted into the "
                "effective edge",
            ))
            first = False
        else:
            traces.append(ArcTrace(
                role, total, item, rec.weight, rec.priority, rec.use, rec.location,
                "merged",
                "identical duplicate edge on the highest priority layer merged into "
                "one edge; the item is counted exactly once",
            ))
    edge = EffectiveEdge(
        role=role, total=total, item=item, weight=weight,
        locations=tuple(m.record.location for m in ordered),
    )
    return edge, traces


def _detect_cycles(role: str, edges: tuple[EffectiveEdge, ...]) -> None:
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        if edge.total == edge.item:
            raise LinkbaseError(
                f"effective calculation graph in role {role!r} contains a self-loop at "
                f"concept {edge.total} (edge declared at {edge.locations[0]})"
            )
        adjacency.setdefault(edge.total, []).append(edge.item)
    for items in adjacency.values():
        items.sort()

    state: dict[str, int] = {}  # 0 = visiting, 1 = done
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 0
        stack.append(node)
        for nxt in adjacency.get(node, ()):
            mark = state.get(nxt)
            if mark == 0:
                start = stack.index(nxt)
                path = stack[start:] + [nxt]
                raise LinkbaseError(
                    "effective calculation graph in role "
                    f"{role!r} contains a cycle: " + " -> ".join(path)
                )
            if mark is None:
                visit(nxt)
        stack.pop()
        state[node] = 1

    for node in sorted(adjacency):
        if state.get(node) is None:
            visit(node)


def resolve_networks(
    linkbase: ParsedLinkbase,
    mapping: dict[str, str],
    concepts: dict[tuple[str, str], object],
) -> tuple[ResolvedNetwork, ...]:
    """Resolve every role network, refusing any structural inconsistency."""
    by_role: dict[str, list[_ResolvedArc]] = {}
    for link in linkbase.links:
        for resolved in _resolve_link(link, mapping, concepts):
            by_role.setdefault(link.role, []).append(resolved)

    networks: list[ResolvedNetwork] = []
    for role in sorted(by_role):
        groups: dict[tuple[str, str], list[_ResolvedArc]] = {}
        for resolved in by_role[role]:
            groups.setdefault((resolved.total, resolved.item), []).append(resolved)
        edges: list[EffectiveEdge] = []
        traces: list[ArcTrace] = []
        for (total, item), members in sorted(groups.items()):
            edge, edge_traces = _merge_relation(role, total, item, members)
            traces.extend(edge_traces)
            if edge is not None:
                edges.append(edge)
        edge_tuple = tuple(sorted(edges, key=lambda e: (e.total, e.item)))
        _detect_cycles(role, edge_tuple)
        traces.sort(key=lambda t: (t.location.line, t.location.column, t.total, t.item))
        networks.append(ResolvedNetwork(role=role, edges=edge_tuple, traces=tuple(traces)))
    return tuple(networks)
