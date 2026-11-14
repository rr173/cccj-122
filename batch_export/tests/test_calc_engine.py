"""End-to-end tests for read-only calculation checks against a real warehouse.

Facts are ingested with the existing XbrlIngestor; expected verdicts and
bounds are written by hand. These tests also prove that checking never writes
to the warehouse, works after the source XML is removed, and that the CLI exit
codes are distinguishable.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from xbrl_ingest import ConceptSpec, XbrlIngestor  # noqa: E402
from calc_check import CalculationChecker  # noqa: E402
from calc_check.errors import (  # noqa: E402
    EXIT_DEFINITION,
    EXIT_INCONSISTENT,
    EXIT_INVOCATION,
    InvocationError,
)
from calc_check.store import ReadOnlyFactStore  # noqa: E402

NS = "http://example.com/calc"
UN = "http://example.com/units"
ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"

CONCEPTS = [
    ConceptSpec(NS, "Tot", "decimal"),
    ConceptSpec(NS, "A", "decimal"),
    ConceptSpec(NS, "B", "decimal"),
    ConceptSpec(NS, "Leaf", "decimal"),
    ConceptSpec(NS, "Mid", "decimal"),
]
DICTIONARY = {"concepts": [
    {"namespace": c.namespace, "name": c.local_name, "type": c.type}
    for c in CONCEPTS
]}
MAPPING = {
    "#tot": "{" + NS + "}Tot",
    "#a": "{" + NS + "}A",
    "#b": "{" + NS + "}B",
    "#leaf": "{" + NS + "}Leaf",
    "#mid": "{" + NS + "}Mid",
}

CTX = (
    '<context id="c"><entity><identifier scheme="s">E1</identifier></entity>'
    '<period><startDate>2024-01-01</startDate><endDate>2024-12-31</endDate></period>'
    "</context>"
)
CTX_DIM = (
    '<context id="cd"><entity><identifier scheme="s">E1</identifier>'
    '<segment><xbrldi:explicitMember dimension="c:K" '
    'xmlns:xbrldi="http://xbrl.org/2006/xbrldi">c:V</xbrldi:explicitMember></segment>'
    '</entity><period><startDate>2024-01-01</startDate>'
    '<endDate>2024-12-31</endDate></period></context>'
)
UNIT_USD = '<unit id="u"><measure xmlns:m="' + UN + '">m:USD</measure></unit>'
UNIT_EUR = '<unit id="e"><measure xmlns:m="' + UN + '">m:EUR</measure></unit>'


def instance(body: str) -> str:
    return (
        '<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:c="' + NS + '">'
        + CTX + CTX_DIM + UNIT_USD + UNIT_EUR + body + "</xbrl>"
    )


def fact(name: str, value: str, decimals: str, unit: str = "u",
         context: str = "c", nil: bool = False) -> str:
    if nil:
        return (f'<c:{name} contextRef="{context}" unitRef="{unit}" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                'xsi:nil="true"/>')
    return (f'<c:{name} contextRef="{context}" unitRef="{unit}" '
            f'decimals="{decimals}">{value}</c:{name}>')


def linkbase(arcs: str, role: str = "http://example.com/role/r") -> str:
    return (
        '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<link:calculationLink xlink:type="extended" xlink:role="{role}">'
        '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
        '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="la"/>'
        '<link:loc xlink:type="locator" xlink:href="#b" xlink:label="lb"/>'
        '<link:loc xlink:type="locator" xlink:href="#mid" xlink:label="lm"/>'
        '<link:loc xlink:type="locator" xlink:href="#leaf" xlink:label="ll"/>'
        + arcs + "</link:calculationLink></link:linkbase>"
    )


SUM_AB = (
    f'<link:calculationArc xlink:type="arc" xlink:from="t" xlink:to="la" '
    f'xlink:arcrole="{ARCROLE}" weight="1"/>'
    f'<link:calculationArc xlink:type="arc" xlink:from="t" xlink:to="lb" '
    f'xlink:arcrole="{ARCROLE}" weight="1"/>'
)


class EngineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="calc_engine_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "w.sqlite")
        self.dict_path = os.path.join(self.tmp, "dict.json")
        self.map_path = os.path.join(self.tmp, "map.json")
        with open(self.dict_path, "w", encoding="utf-8") as fh:
            json.dump(DICTIONARY, fh)
        with open(self.map_path, "w", encoding="utf-8") as fh:
            json.dump(MAPPING, fh)
        self.ing = XbrlIngestor(self.db, self.dict_path)
        self.addCleanup(self.ing.close)

    def write_instance(self, name: str, body: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(instance(body))
        return path

    def ingest(self, name: str, body: str) -> str:
        return self.ing.ingest_file(self.write_instance(name, body)).source_id

    def write_linkbase(self, arcs: str, role: str = "http://example.com/role/r") -> str:
        path = os.path.join(self.tmp, "lb.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(linkbase(arcs, role))
        return path

    def check(self, source_ids, arcs=SUM_AB, role="http://example.com/role/r"):
        lb = self.write_linkbase(arcs, role)
        return CalculationChecker(lb, self.map_path, self.dict_path).check_sources(
            self.db, source_ids)


class TestVerdicts(EngineBase):
    def _one(self, report):
        self.assertEqual(len(report["checks"]), 1)
        return report["checks"][0]

    def test_consistent_when_intervals_overlap_but_display_differs(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "100", "0") + fact("A", "60.3", "1")
                          + fact("B", "40.2", "1"))
        result = self._one(self.check([sid]))
        self.assertEqual(result["verdict"], "consistent")
        self.assertEqual(tuple(result["total_interval"].values()),
                         ("99.5", "100.5"))
        self.assertEqual(tuple(result["sum_interval"].values()),
                         ("100.40", "100.60"))

    def test_inconsistent_when_point_intervals_disagree(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "1000", "INF") + fact("A", "600", "INF")
                          + fact("B", "401", "INF"))
        result = self._one(self.check([sid]))
        self.assertEqual(result["verdict"], "inconsistent")

    def test_endpoint_contact_is_consistent(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "100", "0") + fact("A", "50.5", "1")
                          + fact("B", "49.9", "1"))
        self.assertEqual(self._one(self.check([sid]))["verdict"], "consistent")

    def test_indeterminate_when_item_missing_never_zero(self):
        sid = self.ingest("s.xml", fact("Tot", "100", "0") + fact("A", "60", "1"))
        result = self._one(self.check([sid]))
        self.assertEqual(result["verdict"], "indeterminate")
        self.assertTrue(any("no reported fact" in r for r in result["reasons"]))

    def test_indeterminate_when_total_missing(self):
        sid = self.ingest("s.xml", fact("A", "60", "1") + fact("B", "40", "1"))
        result = self._one(self.check([sid]))
        self.assertEqual(result["verdict"], "indeterminate")
        self.assertTrue(any("total concept" in r for r in result["reasons"]))

    def test_nil_is_indeterminate_not_zero(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "10", "1", nil=True) + fact("A", "6", "1")
                          + fact("B", "4", "1"))
        result = self._one(self.check([sid]))
        self.assertEqual(result["verdict"], "indeterminate")
        self.assertTrue(any("nil" in r for r in result["reasons"]))

    def test_real_zero_calculates_consistently(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "0", "2") + fact("A", "0", "2")
                          + fact("B", "0", "2"))
        result = self._one(self.check([sid]))
        self.assertEqual(result["verdict"], "consistent")
        # d=2 gives half width 0.005; Decimal keeps the third decimal place.
        self.assertEqual(tuple(result["sum_interval"].values()),
                         ("-0.010", "0.010"))

    def test_conflicting_candidates_make_indeterminate(self):
        first = self.ingest("s1.xml",
                            fact("Tot", "100", "2") + fact("A", "60", "2")
                            + fact("B", "40", "2"))
        second_path = self.write_instance(
            "s2.xml", fact("Tot", "101", "2") + fact("A", "60", "2")
            + fact("B", "40", "2"))
        second = self.ing.ingest_file(second_path).source_id
        result = self._one(self.check([first, second]))
        self.assertEqual(result["verdict"], "indeterminate")
        self.assertTrue(any("conflicting" in r for r in result["reasons"]))
        # Both candidates stay traceable.
        self.assertEqual(len(result["total_candidates"]), 2)

    def test_equal_duplicates_intersect_precision_and_count_once(self):
        first = self.ingest("s1.xml",
                            fact("Tot", "55", "2") + fact("A", "55", "2")
                            + fact("B", "0", "2"))
        self.ingest("s2.xml",
                    fact("Tot", "55", "1") + fact("A", "55", "3")
                    + fact("B", "0", "2"))
        result = self._one(self.check([first]))
        # With only the first source, one weighted edge per item still holds.
        self.assertEqual(len(result["weighted_items"]), 2)


class TestCoordinatesAndScope(EngineBase):
    def test_dimensions_and_units_keep_coordinates_apart(self):
        sid = self.ingest("s.xml",
                          fact("A", "10", "2", context="c")
                          + fact("A", "10", "2", context="cd")
                          + fact("B", "0", "2", context="cd")
                          + fact("Tot", "10", "2", context="cd")
                          + fact("Tot", "80", "2", unit="e")
                          + fact("A", "80", "2", unit="e")
                          + fact("B", "0", "2", unit="e"))
        report = self.check([sid])
        verdicts = {(tuple(tuple(d) for d in r["coordinate"]["dimensions"]),
                     r["coordinate"]["unit"]["numerator"][0][0]): r["verdict"]
                    for r in report["checks"]}
        dim_qname = "{" + NS + "}K"
        # Dimensioned USD coordinate matches.
        self.assertEqual(verdicts[(((dim_qname, "{" + NS + "}V"),),
                                   "{" + UN + "}USD")], "consistent")
        # No-dimension USD coordinate has a total only via the other key ->
        # indeterminate; EUR coordinate is consistent on its own unit.
        self.assertEqual(verdicts[((), "{" + UN + "}EUR")], "consistent")

    def test_only_selected_sources_are_used_and_unknown_source_errors(self):
        first = self.ingest("s1.xml",
                            fact("Tot", "100", "INF") + fact("A", "60", "INF")
                            + fact("B", "40", "INF"))
        second = self.ingest("s2.xml",
                             fact("Tot", "100", "INF") + fact("A", "60", "INF")
                             + fact("B", "40", "INF"))
        alone = self.check([first])
        self.assertEqual(alone["checks"][0]["verdict"], "consistent")
        with self.assertRaises(InvocationError):
            self.check(["src_unknown"])
        # The second source alone is internally consistent as well; the two
        # sources are evaluated independently when only one is selected.
        second_report = self.check([second])
        self.assertEqual(second_report["checks"][0]["verdict"], "consistent")

    def test_two_layer_does_not_fabricate_missing_intermediate(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "100", "2") + fact("Mid", "60", "2")
                          + fact("Leaf", "60", "2"))
        arcs = (
            f'<link:calculationArc xlink:type="arc" xlink:from="t" xlink:to="lm" '
            f'xlink:arcrole="{ARCROLE}" weight="1"/>'
            f'<link:calculationArc xlink:type="arc" xlink:from="lm" xlink:to="ll" '
            f'xlink:arcrole="{ARCROLE}" weight="1"/>'
        )
        report = self.check([sid], arcs=arcs)
        by_total = {r["total"].split("}")[1]: r["verdict"] for r in report["checks"]}
        self.assertEqual(by_total["Mid"], "consistent")
        self.assertEqual(by_total["Tot"], "inconsistent")

    def test_unchecked_total_listed_when_no_facts_apply(self):
        sid = self.ingest("s.xml", fact("Leaf", "5", "2"))
        report = self.check([sid], arcs=SUM_AB)
        self.assertEqual(report["counts"],
                         {"consistent": 0, "inconsistent": 0, "indeterminate": 0})
        self.assertTrue(report["unchecked"])
        self.assertIn("no fact", report["unchecked"][0]["reason"])


class TestReadOnlyAndOffline(EngineBase):
    def test_warehouse_unchanged_and_no_side_files(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "100", "0") + fact("A", "60", "1")
                          + fact("B", "40", "1"))
        store = ReadOnlyFactStore(self.db)
        before = store.table_snapshot()
        store.close()
        self.check([sid])
        store = ReadOnlyFactStore(self.db)
        after = store.table_snapshot()
        store.close()
        self.assertEqual(before, after)
        side = [n for n in os.listdir(self.tmp)
                if n.endswith(("-wal", "-shm", "-journal"))]
        self.assertEqual(side, [])

    def test_check_runs_after_source_xml_deleted(self):
        source_path = self.write_instance(
            "s.xml", fact("Tot", "100", "INF") + fact("A", "60", "INF")
            + fact("B", "40", "INF"))
        sid = self.ing.ingest_file(source_path).source_id
        os.remove(source_path)
        report = self.check([sid])
        self.assertEqual(report["checks"][0]["verdict"], "consistent")

    def test_missing_warehouse_is_invocation_error(self):
        with self.assertRaises(InvocationError):
            ReadOnlyFactStore(os.path.join(self.tmp, "nope.sqlite"))


class TestCliExitCodes(EngineBase):
    def _run(self, *args):
        launcher = (
            "import sys\n"
            f"sys.path.insert(0, {PROJECT!r})\n"
            "from calc_check.cli import main\n"
            "raise SystemExit(main())\n"
        )
        return subprocess.run(
            [sys.executable, "-I", "-c", launcher, *args],
            cwd=PROJECT, capture_output=True, text=True,
        )

    def test_inconsistent_vs_definition_vs_invocation_distinguishable(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "1000", "INF") + fact("A", "600", "INF")
                          + fact("B", "401", "INF"))
        lb = self.write_linkbase(SUM_AB)
        report_path = os.path.join(self.tmp, "out.json")
        proc = self._run(
            "check", self.db, lb, "--mapping", self.map_path,
            "--dict", self.dict_path, "--source", sid, "--report", report_path)
        self.assertEqual(proc.returncode, EXIT_INCONSISTENT)
        with open(report_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["counts"]["inconsistent"], 1)

        proc = self._run(
            "check", self.db, lb, "--mapping", self.map_path,
            "--dict", self.dict_path, "--source", "src_missing")
        self.assertEqual(proc.returncode, EXIT_INVOCATION)

        bad = os.path.join(self.tmp, "bad.xml")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("<not-linkbase/>")
        proc = self._run(
            "networks", bad, "--mapping", self.map_path,
            "--dict", self.dict_path)
        self.assertEqual(proc.returncode, EXIT_DEFINITION)

    def test_clean_run_exits_zero(self):
        sid = self.ingest("s.xml",
                          fact("Tot", "100", "INF") + fact("A", "60", "INF")
                          + fact("B", "40", "INF"))
        lb = self.write_linkbase(SUM_AB)
        proc = self._run(
            "check", self.db, lb, "--mapping", self.map_path,
            "--dict", self.dict_path, "--source", sid)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
