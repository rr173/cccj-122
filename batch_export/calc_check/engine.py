"""The calculation checking engine.

For every resolved network role and every total concept, the engine takes the
union of business coordinates at which the total or any of its *direct* items
was reported across the explicitly selected sources, and returns one verdict
per coordinate:

* ``consistent``   -- the weighted interval sum of the direct items meets the
  total's effective interval (endpoint contact counts);
* ``inconsistent`` -- every side is fully determined and the closed intervals
  are disjoint;
* ``indeterminate`` -- a total or item is missing, nil, or has conflicting
  candidates. Missing values are never treated as zero and no candidate is ever
  picked at random.

Only direct, actually reported item facts are used: a missing intermediate total
is never computed and fed into an upper-level check.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .intervals import (
    DecimalInterval,
    add_intervals,
    fact_interval,
    intersect_intervals,
    intervals_overlap,
    scale_interval,
)
from .network import EffectiveEdge, ResolvedNetwork
from .store import FactOccurrence

VERDICTS = ("consistent", "inconsistent", "indeterminate")


@dataclass(frozen=True)
class _ItemGroups:
    total_group: Optional[list[FactOccurrence]]
    items: dict[str, list[FactOccurrence]]


def candidate_entry(fact: FactOccurrence) -> dict:
    return {
        "source_id": fact.source_id,
        "position": fact.position,
        "line": fact.line,
        "column": fact.column,
        "nil": fact.nil,
        "decimals": fact.decimals,
        "value": None if fact.value is None else format(fact.value, "f"),
    }


def _coordinate_key(fact: FactOccurrence) -> tuple[str, Optional[str]]:
    return fact.context_digest, fact.unit_digest


def _effective_interval(group: list[FactOccurrence]) -> tuple[DecimalInterval, list[dict]]:
    """Intersect the precision intervals of equal-valued duplicate candidates."""
    interval = fact_interval(group[0].value, group[0].decimals)
    precision = [{
        "decimals": group[0].decimals,
        "interval": interval.as_text(),
        "candidate": candidate_entry(group[0]),
    }]
    for fact in group[1:]:
        next_interval = fact_interval(fact.value, fact.decimals)
        interval = intersect_intervals(interval, next_interval)
        precision.append({
            "decimals": fact.decimals,
            "interval": next_interval.as_text(),
            "candidate": candidate_entry(fact),
        })
    return interval, precision


class CheckEngine:
    def __init__(self, networks: tuple[ResolvedNetwork, ...],
                 facts: list[FactOccurrence]):
        self.networks = networks
        self._by_concept: dict[str, dict[tuple[str, Optional[str]], list[FactOccurrence]]] = {}
        for fact in facts:
            concept_map = self._by_concept.setdefault(fact.concept, {})
            concept_map.setdefault(_coordinate_key(fact), []).append(fact)

    def run(self) -> dict:
        results: list[dict] = []
        unchecked: list[dict] = []
        for network in self.networks:
            for total in network.totals:
                edges = network.edges_for_total(total)
                items = tuple(edge.item for edge in edges)
                coordinate_keys = self._coordinate_union(total, items)
                if not coordinate_keys:
                    unchecked.append({
                        "role": network.role,
                        "total": total,
                        "reason": "no fact for the total or any direct item exists in the "
                                  "selected sources; nothing could be checked",
                        "edges": [self._edge_text(edge) for edge in edges],
                    })
                    continue
                for key in sorted(coordinate_keys, key=lambda k: self._key_text(total, k)):
                    results.append(self._check_coordinate(network, total, edges, key))
        counts = {verdict: 0 for verdict in VERDICTS}
        for result in results:
            counts[result["verdict"]] += 1
        return {
            "checks": results,
            "unchecked": unchecked,
            "counts": counts,
        }

    # -- coordinate discovery --------------------------------------------------

    def _coordinate_union(
        self, total: str, items: tuple[str, ...]
    ) -> set[tuple[str, Optional[str]]]:
        keys: set[tuple[str, Optional[str]]] = set()
        for concept in (total, *items):
            keys.update(self._by_concept.get(concept, {}).keys())
        return keys

    def _key_text(self, concept: str, key: tuple[str, Optional[str]]) -> str:
        group = self._by_concept.get(concept, {}).get(key)
        if group:
            return _canonical_coordinate_text(group[0].coordinate)
        for concept_map in self._by_concept.values():
            group = concept_map.get(key)
            if group:
                return _canonical_coordinate_text(group[0].coordinate)
        return repr(key)

    # -- one coordinate --------------------------------------------------------

    def _gather(self, total: str, items: tuple[str, ...],
                key: tuple[str, Optional[str]]) -> _ItemGroups:
        total_group = self._by_concept.get(total, {}).get(key)
        item_groups = {
            item: self._by_concept.get(item, {}).get(key, []) for item in items
        }
        return _ItemGroups(total_group=total_group, items=item_groups)

    def _check_coordinate(
        self,
        network: ResolvedNetwork,
        total: str,
        edges: tuple[EffectiveEdge, ...],
        key: tuple[str, Optional[str]],
    ) -> dict:
        item_concepts = tuple(edge.item for edge in edges)
        gathered = self._gather(total, item_concepts, key)
        reasons: list[str] = []

        coordinate = self._coordinate_text(total, item_concepts, key)
        result: dict = {
            "role": network.role,
            "total": total,
            "coordinate": coordinate,
            "edges": [self._edge_text(edge) for edge in edges],
            "total_candidates": [],
            "item_candidates": {},
        }

        # -- total side --------------------------------------------------------
        total_interval: Optional[DecimalInterval] = None
        total_precision: list[dict] = []
        if gathered.total_group is None:
            reasons.append("total concept has no reported fact at this coordinate")
        else:
            result["total_candidates"] = [
                candidate_entry(fact) for fact in gathered.total_group
            ]
            nils = [fact for fact in gathered.total_group if fact.nil]
            if nils:
                reasons.append(
                    "total is nil at this coordinate ("
                    + ", ".join(f"{f.source_id} position {f.position}" for f in nils)
                    + ")"
                )
            values = {format(fact.value, "f") for fact in gathered.total_group if not fact.nil}
            if len(values) > 1:
                reasons.append(
                    "total has conflicting fact candidates with different values: "
                    + ", ".join(sorted(values))
                )
            elif values and not nils:
                total_interval, total_precision = _effective_interval(gathered.total_group)

        # -- item side ---------------------------------------------------------
        weighted: list[dict] = []
        for edge in edges:
            group = gathered.items.get(edge.item, [])
            label = self._edge_text(edge)
            if not group:
                reasons.append(f"direct item {edge.item} has no reported fact at this coordinate")
                result["item_candidates"].setdefault(edge.item, [])
                continue
            result["item_candidates"][edge.item] = [candidate_entry(fact) for fact in group]
            item_nils = [fact for fact in group if fact.nil]
            item_values = {format(fact.value, "f") for fact in group if not fact.nil}
            if item_nils:
                reasons.append(
                    f"item {edge.item} is nil ("
                    + ", ".join(f"{f.source_id} position {f.position}" for f in item_nils)
                    + ")"
                )
                continue
            if len(item_values) > 1:
                reasons.append(
                    f"item {edge.item} has conflicting fact candidates with different "
                    "values: " + ", ".join(sorted(item_values))
                )
                continue
            item_interval, precision = _effective_interval(group)
            weighted_interval = scale_interval(item_interval, edge.weight)
            weighted.append({
                "edge": label,
                "weight": format(edge.weight, "f"),
                "candidate_count": len(group),
                "candidate_precision": precision,
                "source_interval": item_interval.as_text(),
                "weighted_interval": weighted_interval.as_text(),
            })

        # -- verdict -----------------------------------------------------------
        if reasons:
            result["verdict"] = "indeterminate"
            result["reasons"] = reasons
            result["total_interval"] = (
                total_interval.as_text() if total_interval is not None else None
            )
            result["total_precision"] = total_precision
            result["weighted_items"] = weighted
            result["sum_interval"] = None
            result["explanation"] = (
                "at least one required value is missing, nil or conflicting; no zero was "
                "imputed and no candidate was selected arbitrarily"
            )
            return result

        weighted_intervals = [
            DecimalInterval(
                Decimal(entry["weighted_interval"]["lower"]),
                Decimal(entry["weighted_interval"]["upper"]),
            )
            for entry in weighted
        ]
        sum_interval = add_intervals(weighted_intervals)
        overlaps = intervals_overlap(total_interval, sum_interval)
        result["verdict"] = "consistent" if overlaps else "inconsistent"
        result["reasons"] = []
        result["total_interval"] = total_interval.as_text()
        result["total_precision"] = total_precision
        result["weighted_items"] = weighted
        result["sum_interval"] = sum_interval.as_text()
        if overlaps:
            result["explanation"] = (
                "the closed precision interval of the total intersects the sum of the "
                "weighted direct-item intervals (endpoint contact counts as compatible)"
            )
        else:
            result["explanation"] = (
                "the closed precision interval of the total and the weighted sum interval "
                "are disjoint under the declared decimals rule"
            )
        return result

    # -- presentation helpers --------------------------------------------------

    @staticmethod
    def _edge_text(edge: EffectiveEdge) -> dict:
        return {
            "role": edge.role,
            "total": edge.total,
            "item": edge.item,
            "weight": format(edge.weight, "f"),
            "declared_at": [str(loc) for loc in edge.locations],
        }

    def _coordinate_text(
        self,
        total: str,
        items: tuple[str, ...],
        key: tuple[str, Optional[str]],
    ) -> dict:
        for concept in (total, *items):
            group = self._by_concept.get(concept, {}).get(key)
            if group:
                coordinate = dict(group[0].coordinate["context"])
                coordinate["unit"] = group[0].coordinate["unit"]
                return coordinate
        raise AssertionError(f"coordinate key {key!r} has no fact anywhere")  # pragma: no cover


def _canonical_coordinate_text(coordinate: dict) -> str:
    import json
    return json.dumps(coordinate, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
