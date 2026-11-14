"""Public API for read-only calculation relationship checks."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from .engine import CheckEngine
from .errors import InvocationError, LinkbaseError
from .linkbase import load_locator_mapping, parse_linkbase_file
from .network import ResolvedNetwork, resolve_networks
from .store import ReadOnlyFactStore
from xbrl_ingest.dictionary import DictionaryError, load_dictionary

PRECISION_NOTICE = (
    "verdicts apply only to the explicit closed-interval decimals rule "
    "[v-0.5*10^-d, v+0.5*10^-d], INF = point interval; they do not prove the "
    "financial truth of the reported numbers"
)


class CalculationChecker:
    """Parse and resolve a local calculation linkbase, then check selected facts.

    The linkbase, mapping and concept dictionary are validated eagerly in
    ``__init__``, so an invalid relationship definition fails before any fact
    warehouse is opened. The warehouse is opened read-only and never written to.
    """

    def __init__(self, linkbase_path: str, mapping_path: str, dictionary):
        try:
            self._dictionary = load_dictionary(dictionary)
        except DictionaryError as exc:
            raise InvocationError(f"concept dictionary error: {exc}") from None
        self.linkbase = parse_linkbase_file(linkbase_path)
        self.mapping = load_locator_mapping(mapping_path)
        self.networks: tuple[ResolvedNetwork, ...] = resolve_networks(
            self.linkbase, self.mapping, self._dictionary
        )

    def validate_definition(self) -> dict:
        """Inspect resolved networks without touching a fact warehouse."""
        return {
            "linkbase": self.linkbase.path,
            "networks": [
                {
                    "role": network.role,
                    "effective_edges": [
                        {
                            "total": edge.total,
                            "item": edge.item,
                            "weight": format(edge.weight, "f"),
                            "declared_at": [str(loc) for loc in edge.locations],
                        }
                        for edge in network.edges
                    ],
                    "arc_traces": [trace.as_text() for trace in network.traces],
                    "totals": list(network.totals),
                }
                for network in self.networks
            ],
        }

    def check_sources(
        self, db_path: str, source_ids: Sequence[str]
    ) -> dict:
        """Check the selected sources against the resolved networks."""
        store = ReadOnlyFactStore(db_path)
        try:
            store.require_sources(source_ids)
            facts = store.load_facts(source_ids)
            report = CheckEngine(self.networks, facts).run()
        finally:
            store.close()
        report = dict(report)
        report["warehouse"] = str(Path(db_path))
        report["selected_sources"] = list(dict.fromkeys(source_ids))
        report["networks"] = self._network_summary()
        report["precision_notice"] = PRECISION_NOTICE
        return report

    def available_sources(self, db_path: str) -> list[str]:
        store = ReadOnlyFactStore(db_path)
        try:
            return store.source_ids()
        finally:
            store.close()

    def _network_summary(self) -> list[dict]:
        return [
            {"role": network.role, "totals": list(network.totals),
             "edge_count": len(network.edges)}
            for network in self.networks
        ]
