#!/usr/bin/env python3
"""Runnable demonstration of read-only calculation relationship checking.

Flow:

1. Ingest three hand-written instance files into a throwaway warehouse using
   the *existing* ingestion entry point (unchanged).
2. Print the human expectations derived by hand in
   expected/calc_expectations.py.
3. Run the ``calc_check.cli`` terminal command to produce a machine-readable
   JSON report, then read that report back and assert every verdict, interval
   bound, effective edge and source/position trace against the hand-written
   literals.
4. Exercise the definition-failure linkbases and the invocation/exit-code
   distinctions, and prove the warehouse is byte-identical before and after.

Exit 0 means every hand-written expectation held.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "calc_fixtures")
sys.path.insert(0, PROJECT)
sys.path.insert(0, os.path.join(HERE, "expected"))

import calc_expectations as X  # noqa: E402  (hand-written, independent)
from xbrl_ingest import XbrlIngestor  # noqa: E402
from calc_check import CalculationChecker  # noqa: E402
from calc_check.errors import (  # noqa: E402
    EXIT_DEFINITION,
    EXIT_INCONSISTENT,
    EXIT_INVOCATION,
)


class Checker:
    def __init__(self, verbose: bool):
        self.verbose = verbose
        self.failures: list[str] = []
        self.checks = 0

    def check(self, label: str, actual, expected) -> None:
        self.checks += 1
        if actual != expected:
            self.failures.append(f"{label}: expected {expected!r}, got {actual!r}")
            print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
        elif self.verbose:
            print(f"  ok   {label} = {actual!r}")

    def check_true(self, label: str, condition: bool, detail: str = "") -> None:
        self.check(label, bool(condition), True)
        if not condition and detail:
            print(f"        {detail}")

    def finish(self) -> int:
        if self.failures:
            print(f"\n{len(self.failures)} of {self.checks} checks failed")
            return 1
        print(f"\nall {self.checks} hand-written expectations held")
        return 0


def run_cli(*cli_args: str) -> subprocess.CompletedProcess:
    # Isolated mode (-I) ignores both cwd and PYTHONPATH, so a tiny trusted
    # launcher inserts the explicit project path before importing the CLI.
    launcher = (
        "import sys\n"
        f"sys.path.insert(0, {PROJECT!r})\n"
        "from calc_check.cli import main\n"
        "raise SystemExit(main())\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", launcher, *cli_args],
        cwd=PROJECT, capture_output=True, text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    checker = Checker(args.verbose)
    workdir = tempfile.mkdtemp(prefix="calc_demo_")
    try:
        db = os.path.join(workdir, "warehouse.sqlite")
        dictionary = os.path.join(FIXTURES, "concepts.json")
        mapping = os.path.join(FIXTURES, "locator_mapping.json")
        linkbase = os.path.join(FIXTURES, "calculations.xml")

        print("1. Ingest the three demo sources through the existing entry point")
        ing = XbrlIngestor(db, dictionary)
        source_ids = []
        for n in (1, 2, 3):
            report = ing.ingest_file(os.path.join(FIXTURES, f"calc-source-{n}.xml"))
            source_ids.append(report.source_id)
            print(f"   {report.source_id} <- calc-source-{n}.xml "
                  f"({report.fact_count} facts)")
        # Snapshot every table through the read-only store so the ingestion API
        # itself is not modified.
        from calc_check.store import ReadOnlyFactStore
        ro = ReadOnlyFactStore(db)
        snapshot_before = ro.table_snapshot()
        ro.close()
        ing.close()

        print("\n2. Human expectations (derived by hand, not from production code)")
        for total, entry in X.S12_EXPECTATIONS.items():
            if isinstance(entry[0], tuple):
                print(f"   {total:<22} coordinate rule {entry}")
            else:
                print(f"   {total:<22} {entry[0]}")

        print("\n3. Resolve networks and assert edges / arc fates")
        definition = CalculationChecker(linkbase, mapping, dictionary)
        edge_counts = {net.role: len(net.edges) for net in definition.networks}
        checker.check("edge counts per role", edge_counts, X.EXPECTED_EDGE_COUNTS)
        for net in definition.networks:
            if net.role == X.ROLE_OVERRIDE:
                edge = net.edges[0]
                checker.check("override total", edge.total, X.OVERRIDE_TOTAL)
                checker.check("override item", edge.item, X.OVERRIDE_ITEM)
                checker.check("override effective weight",
                              format(edge.weight, "f"), X.OVERRIDE_WEIGHT)
                dispositions = tuple(t.disposition for t in net.traces)
                checker.check("override dispositions", dispositions,
                              X.OVERRIDE_DISPOSITIONS)
            if net.role == X.ROLE_PROHIBITION:
                checker.check("prohibition leaves no edge", len(net.edges), 0)
                dispositions = tuple(t.disposition for t in net.traces)
                checker.check("prohibition trace dispositions", dispositions,
                              X.PROHIBITION_TRACE_DISPOSITIONS)
            if net.role == X.ROLE_BROKEN_CYCLE:
                dispositions = tuple(t.disposition for t in net.traces)
                checker.check("broken-cycle dispositions", dispositions,
                              X.BROKEN_CYCLE_DISPOSITIONS)
        # The duplicate Dup edge must merge: exactly one edge, two locations.
        interval_net = next(n for n in definition.networks
                            if n.role == X.ROLE_INTERVAL)
        dup_edge = next(e for e in interval_net.edges
                        if e.total.endswith("}DupTotal"))
        checker.check("duplicate edge merged to one",
                      len(interval_net.edges), X.EXPECTED_EDGE_COUNTS[X.ROLE_INTERVAL])
        checker.check("merged edge keeps both declarations",
                      len(dup_edge.locations), 2)

        print("\n4. CLI check on S1+S2 and read back the JSON report")
        report_path = os.path.join(workdir, "report-s12.json")
        proc = run_cli(
            "check", db, linkbase, "--mapping", mapping, "--dict", dictionary,
            "--source", source_ids[0], "--source", source_ids[1],
            "--report", report_path,
        )
        print(f"   exit status = {proc.returncode}")
        checker.check("S1+S2 run exits 4 (real inconsistencies exist)",
                      proc.returncode, EXIT_INCONSISTENT)
        report = json.load(open(report_path, encoding="utf-8"))
        checker.check("S1+S2 counts", report["counts"], X.S12_COUNTS)
        checker.check("report names both sources",
                      report["selected_sources"], source_ids[:2])
        checker.check_true("report carries the precision notice",
                           "closed-interval" in report["precision_notice"])

        by_total: dict[str, list[dict]] = {}
        for result in report["checks"]:
            by_total.setdefault(result["total"].split("}")[1], []).append(result)

        for total, entry in X.S12_EXPECTATIONS.items():
            # Coordinate- and unit-specific rules are asserted separately below.
            if not (len(entry) == 3 and isinstance(entry[0], str)
                    and entry[0] in ("consistent", "inconsistent", "indeterminate")):
                continue
            verdict, total_bounds, sum_bounds = entry
            results = by_total.get(total, [])
            matching = [r for r in results if r["verdict"] == verdict]
            checker.check_true(f"{total} has a {verdict} result", bool(matching),
                               json.dumps(results, ensure_ascii=False))
            if matching and total_bounds is not None:
                result = matching[0]
                checker.check(f"{total} total interval",
                              tuple(result["total_interval"].values()), total_bounds)
                checker.check(f"{total} sum interval",
                              tuple(result["sum_interval"].values()), sum_bounds)

        print("\n5. Specific coordinate and unit isolation rules")
        dim_results = by_total["DimTotal"]
        dim_verdicts = {}
        for result in dim_results:
            # Coordinates expose each dimension as a full [name, member] pair.
            dims = tuple(tuple(pair) for pair in result["coordinate"]["dimensions"])
            dim_verdicts.setdefault(dims, set()).add(result["verdict"])
        checker.check("DimTotal Online consistent",
                      dim_verdicts.get(
                          X.S12_EXPECTATIONS["DimTotal_consistent_coordinate_dims"]),
                      {"consistent"})
        for dims in X.S12_EXPECTATIONS["DimTotal_indeterminate_coordinate_dims"]:
            checker.check(f"DimTotal dims={dims} indeterminate",
                          dim_verdicts.get(dims), {"indeterminate"})
        eur_results = by_total["EurTotal"]
        units = {
            tuple(result["coordinate"]["unit"]["numerator"][0])[0]:
                result["verdict"]
            for result in eur_results
        }
        checker.check("EUR coordinate consistent", units.get(X.EUR), "consistent")
        checker.check("USD coordinate never paired (indeterminate)",
                      units.get(X.USD), "indeterminate")

        print("\n6. Duplicate equal facts count as one weighted item")
        dup = next(r for r in by_total["DupTotal"] if r["verdict"] == "consistent")
        checker.check("Dup weighted item count", len(dup["weighted_items"]), 1)
        weighted = dup["weighted_items"][0]
        checker.check("Dup candidate facts retained",
                      weighted["candidate_count"], 2)
        positions = tuple(
            c["position"] for c in dup["item_candidates"][
                "{http://example.com/calc}DupItem"]
        )
        checker.check("Dup item source positions", positions,
                      X.DUP_CANDIDATE_POSITIONS["DupItem"])

        print("\n7. Scope: S1 alone, then S1+S2+S3 conflicts")
        rep1 = CalculationChecker(linkbase, mapping, dictionary).check_sources(
            db, [source_ids[0]])
        checker.check("S1-only counts", rep1["counts"], X.S1_COUNTS)
        rep_all = CalculationChecker(linkbase, mapping, dictionary).check_sources(
            db, source_ids)
        checker.check("all-source counts", rep_all["counts"], X.ALL_COUNTS)
        all_by_total = {}
        for result in rep_all["checks"]:
            all_by_total.setdefault(result["total"].split("}")[1], result)
        for total in X.S3_CONFLICT_TOTALS:
            checker.check(f"{total} flips to indeterminate with S3",
                          all_by_total[total]["verdict"], "indeterminate")

        print("\n8. Definition failures exit with the definition status")
        for filename, needle in (
            ("bad-weight-clash.xml", "conflicting weights"),
            ("bad-dangling.xml", "no matching link:loc"),
            ("bad-cycle.xml", "contains a cycle"),
            ("bad-arcrole.xml", "summation-item"),
            ("bad-href.xml", "same-document"),
            ("bad-duplicate-label.xml", "duplicate xlink:label"),
            ("bad-self-loop.xml", "self-loop"),
        ):
            proc = run_cli(
                "networks", os.path.join(FIXTURES, filename),
                "--mapping", mapping, "--dict", dictionary,
            )
            checker.check(f"{filename} definition exit",
                          proc.returncode, EXIT_DEFINITION)
            checker.check_true(f"{filename} message locates problem",
                               needle in proc.stderr, proc.stderr)

        print("\n9. Invocation errors and a fully-consistent clean exit")
        proc = run_cli(
            "check", db, linkbase, "--mapping", mapping, "--dict", dictionary,
            "--source", "src_does_not_exist",
        )
        checker.check("unknown source invocation exit",
                      proc.returncode, EXIT_INVOCATION)
        missing_db = os.path.join(workdir, "no-such.sqlite")
        proc = run_cli(
            "check", missing_db, linkbase, "--mapping", mapping,
            "--dict", dictionary, "--source", source_ids[0],
        )
        checker.check("missing warehouse invocation exit",
                      proc.returncode, EXIT_INVOCATION)

        print("\n10. Warehouse tables are unchanged by every check")
        ro = ReadOnlyFactStore(db)
        snapshot_after = ro.table_snapshot()
        ro.close()
        checker.check("row counts identical before/after",
                      snapshot_after, snapshot_before)
        # And no journal/wal side files were created by a read-only open.
        side_files = [name for name in os.listdir(workdir)
                      if name.endswith(("-wal", "-shm", "-journal"))]
        checker.check("no SQLite side files created", side_files, [])

        print("\n11. Unchecked totals are reported (purpose-built empty network)")
        # A network whose concepts are absent from the selected source must be
        # listed under "unchecked" rather than silently counted as passing.
        _build_unchecked_case(workdir, dictionary, mapping)
        unchecked_report_path = os.path.join(workdir, "unchecked.json")
        proc = run_cli(
            "check", os.path.join(workdir, "unchecked.sqlite"),
            os.path.join(workdir, "unchecked.xml"),
            "--mapping", mapping, "--dict", dictionary,
            "--source", _unchecked_source_id(workdir),
            "--report", unchecked_report_path,
        )
        unchecked_report = json.load(open(unchecked_report_path, encoding="utf-8"))
        checker.check_true("empty network lists an unchecked total",
                           any(item["total"].endswith("}IsolatedTotal")
                               for item in unchecked_report["unchecked"]))
        checker.check("fully-indeterminate run exits 0", proc.returncode, 0)

        return checker.finish()
    finally:
        shutil.rmtree(workdir, True)


def _build_unchecked_case(workdir: str, dictionary: str, mapping: str) -> None:
    """A network whose concepts have no facts: the total must be listed, not
    silently passed."""
    instance = os.path.join(workdir, "unchecked-instance.xml")
    with open(instance, "w", encoding="utf-8") as handle:
        handle.write(
            '<xbrl xmlns="http://www.xbrl.org/2003/instance" '
            'xmlns:c="http://example.com/calc" xmlns:u="http://example.com/units">'
            '<context id="c"><entity><identifier scheme="s">E</identifier></entity>'
            '<period><instant>2024-12-31</instant></period></context>'
            '<unit id="m"><measure>u:USD</measure></unit>'
            '<c:ZeroTotal contextRef="c" unitRef="m" decimals="2">0</c:ZeroTotal>'
            '<c:ZeroItem contextRef="c" unitRef="m" decimals="2">0</c:ZeroItem>'
            "</xbrl>"
        )
    linkbase = os.path.join(workdir, "unchecked.xml")
    with open(linkbase, "w", encoding="utf-8") as handle:
        handle.write(
            '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<link:calculationLink xlink:type="extended" '
            'xlink:role="http://example.com/role/empty">'
            '<link:loc xlink:type="locator" xlink:href="#isolated_total" '
            'xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#isolated_item" '
            'xlink:label="i"/>'
            '<link:calculationArc xlink:type="arc" xlink:from="t" xlink:to="i" '
            'xlink:arcrole="http://www.xbrl.org/2003/arcrole/summation-item" '
            'weight="1"/></link:calculationLink></link:linkbase>'
        )
    ing = XbrlIngestor(os.path.join(workdir, "unchecked.sqlite"), dictionary)
    report = ing.ingest_file(instance)
    ing.close()
    with open(os.path.join(workdir, "unchecked-source.txt"), "w",
              encoding="utf-8") as handle:
        handle.write(report.source_id)


def _unchecked_source_id(workdir: str) -> str:
    return open(os.path.join(workdir, "unchecked-source.txt"),
                encoding="utf-8").read().strip()


if __name__ == "__main__":
    raise SystemExit(main())
