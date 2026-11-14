#!/usr/bin/env python3
"""Runnable demonstration of the local XBRL ingester.

It ingests the demo fixtures into a throwaway SQLite warehouse, then compares
the *actual* warehouse contents against ../expected/demo_expectations.py, which
was written by hand from the XML and is never produced by calling the
ingester's canonicalisation helpers.

Run from anywhere:

    python3 demo/run_demo.py            # fresh temp DB, full walkthrough
    python3 demo/run_demo.py --verbose  # also print every candidate

Exit status 0 means every hand-written expectation held.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "xbrl_fixtures")
sys.path.insert(0, PROJECT)
sys.path.insert(0, os.path.join(HERE, "expected"))

import demo_expectations as X  # noqa: E402  (hand-written, independent)
from xbrl_ingest import IngestError, XbrlIngestor, XbrlStore  # noqa: E402


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

    def finish(self) -> int:
        if self.failures:
            print(f"\n{len(self.failures)} of {self.checks} checks failed")
            return 1
        print(f"\nall {self.checks} hand-written expectations held")
        return 0


def show_ingest(report) -> None:
    print(
        f"  {report.outcome:<16} {os.path.basename(report.path):<16}"
        f" facts={report.fact_count} contexts={report.context_count}"
        f" units={report.unit_count} source={report.source_id}"
    )
    for warning in report.warnings:
        print(f"    warning: {warning}")


def describe(result) -> str:
    coordinate = result.coordinate
    period = coordinate.context.period
    period_text = (
        f"instant {period.instant}" if period.kind == "instant"
        else f"duration {period.start}..{period.end}"
    )
    dimensions = ", ".join(
        f"{name.split('}')[1]}={member.split('}')[1]}"
        for name, member in coordinate.context.dimensions
    ) or "-"
    unit = "-"
    if coordinate.unit is not None:
        unit = " * ".join(t.measure.split("}")[1] for t in coordinate.unit.numerator)
        if coordinate.unit.denominator:
            unit += " / " + " * ".join(
                t.measure.split("}")[1] for t in coordinate.unit.denominator
            )
    return (
        f"{coordinate.concept.split('}')[1]} | {period_text} | dims={dimensions}"
        f" | unit={unit} | lang={coordinate.language}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    checker = Checker(args.verbose)

    workdir = tempfile.mkdtemp(prefix="xbrl_demo_")
    fixtures = os.path.join(workdir, "fixtures")
    shutil.copytree(FIXTURES, fixtures)
    db_path = os.path.join(workdir, "warehouse.sqlite")
    dictionary = os.path.join(fixtures, "concepts.json")
    dictionary_copy = os.path.join(workdir, "concepts.json")
    shutil.copy(dictionary, dictionary_copy)

    try:
        print("1. Ingest demo instances A-D")
        ingestor = XbrlIngestor(db_path, dictionary)
        reports = {}
        for name in ("demo-A.xml", "demo-B.xml", "demo-C.xml", "demo-D.xml"):
            report = ingestor.ingest_file(os.path.join(fixtures, name))
            reports[name] = report
            show_ingest(report)
        checker.check("source count after A-D", ingestor.store.source_count(), 4)

        print("\n2. Values, positions, nil and text (hand-written expectations)")
        for name, expected in (("demo-A.xml", X.DEMO_A), ("demo-B.xml", X.DEMO_B),
                               ("demo-D.xml", X.DEMO_D)):
            facts = ingestor.facts_for_source(reports[name].source_id)
            checker.check(f"{name} fact count", len(facts), expected["fact_count"])
            for row, want in zip(facts, expected["facts"]):
                position, concept, value_text, nil, language, unit_measures = want
                checker.check(f"{name} pos {position} concept", row.concept, concept)
                checker.check(f"{name} pos {position} text", row.value_text, value_text)
                checker.check(f"{name} pos {position} nil", row.nil, nil)
                checker.check(f"{name} pos {position} language", row.language, language)
                if unit_measures is None:
                    checker.check(f"{name} pos {position} unit", row.coordinate.unit, None)
                else:
                    actual = tuple(t.measure for t in row.coordinate.unit.numerator)
                    wanted = tuple("{" + X.UNITS + "}" + m for m in unit_measures)
                    checker.check(f"{name} pos {position} unit", actual, wanted)

        print("\n3. Business coordinates across files")
        duration = ingestor.query(concept=X.REVENUE, period_kind="duration")
        checker.check("duration Revenue result count", len(duration), 1)
        duration_result = duration[0]
        context = duration_result.coordinate.context
        checker.check("duration entity scheme", context.entity.scheme, X.LEI)
        checker.check("duration entity identifier", context.entity.identifier, "LEI-001")
        checker.check("duration start", context.period.start, "2024-01-01")
        checker.check("duration end", context.period.end, "2024-12-31")
        checker.check("duration dimensions", context.dimensions, X.DIMS_ALPHA)
        checker.check("duration status", duration_result.status, "conflict")
        checker.check("duration candidate count", len(duration_result.candidates), 2)
        checker.check(
            "duration candidates",
            sorted(f.value_text for f in duration_result.candidates),
            ["1234.50", "1250.00"],
        )
        checker.check("duration contributing sources", len(duration_result.sources), 2)

        instant = ingestor.query(
            concept=X.REVENUE, period_kind="instant", entity_scheme=X.LEI
        )
        checker.check("instant Revenue result count", len(instant), 1)
        instant_result = instant[0]
        checker.check("instant status", instant_result.status, "single")
        checker.check(
            "instant candidates (1.0 / 1.00 / 1.000 kept separately)",
            [f.value_text for f in instant_result.candidates],
            ["1.0", "1.00", "1.000"],
        )
        checker.check(
            "instant candidate positions",
            [f.position for f in instant_result.candidates],
            [3, 4, 2],
        )
        checker.check("instant sources", len({f.source_id for f in instant_result.candidates}), 2)

        print("\n4. Unit identity: order ignored, direction and repeats kept")
        tiny = ingestor.query(concept=X.TINY)[0]
        checker.check(
            "USD*shares merged across measure order (candidate count)",
            len(tiny.candidates), 2,
        )
        a_units = {uid: coord for uid, coord, _ in ingestor.units_for_source(
            reports["demo-A.xml"].source_id)}
        b_units = {uid: coord for uid, coord, _ in ingestor.units_for_source(
            reports["demo-B.xml"].source_id)}
        checker.check(
            "A u-ratio numerator", tuple(t.measure for t in a_units["u-ratio"].numerator),
            (X.SHARES,),
        )
        checker.check(
            "A u-ratio denominator", tuple(t.measure for t in a_units["u-ratio"].denominator),
            (X.LEGACY_USD,),
        )
        checker.check(
            "B u-ratio numerator", tuple(t.measure for t in b_units["u-ratio"].numerator),
            (X.USD,),
        )
        checker.check(
            "B u-ratio denominator", tuple(t.measure for t in b_units["u-ratio"].denominator),
            (X.SHARES,),
        )
        checker.check(
            "reverse divide units are distinct",
            a_units["u-ratio"].digest == b_units["u-ratio"].digest, False,
        )

        print("\n5. Entity scheme separates coordinates")
        assets = ingestor.query(concept=X.ASSETS)
        checker.check("Assets result count", len(assets), 2)
        checker.check(
            "Assets schemes",
            sorted(r.coordinate.context.entity.scheme for r in assets),
            [X.INTERNAL, X.LEI],
        )

        print("\n6. Text language participates in the coordinate")
        notes = ingestor.query(concept_local="Note")
        checker.check(
            "Note languages", sorted(r.coordinate.language for r in notes),
            ["de", "en", "en", "fr"],
        )
        en = [r for r in notes if r.coordinate.language == "en"
              and r.coordinate.context.entity.identifier == "LEI-002"][0]
        checker.check("LEI-002 en Note status", en.status, "conflict")
        checker.check(
            "LEI-002 en Note candidates", sorted(f.value_text for f in en.candidates),
            ["alpha", "beta"],
        )

        print("\n7. Full listing of every query coordinate")
        for result in ingestor.query():
            print(f"  [{result.status:<8}] {describe(result)} "
                  f"candidates={len(result.candidates)}")
            if args.verbose:
                for fact in result.candidates:
                    print(f"      {fact.source_id} pos {fact.position} "
                          f"value={fact.value_text!r} decimals={fact.decimals}"
                          f" at {fact.location}")

        print("\n8. Refused files: whole file, no partial facts")
        before = (ingestor.store.source_count(), ingestor.store.fact_row_count())
        checker.check("rows before refusals", before, (4, 19))
        for name, error_name, needle in X.REFUSED:
            try:
                ingestor.ingest_file(os.path.join(fixtures, name))
                checker.check(f"{name} refused", "accepted", "refused")
            except IngestError as exc:
                checker.check(f"{name} error type", type(exc).__name__, error_name)
                checker.check(f"{name} names file", os.path.join(fixtures, name) in str(exc), True)
                checker.check(
                    f"{name} names location",
                    bool(__import__("re").search(r":\d+:\d+:", str(exc))), True,
                )
                checker.check(f"{name} message", needle in str(exc), True)
                if args.verbose:
                    print(f"      {exc}")
        checker.check(
            "counts unchanged after all refusals",
            (ingestor.store.source_count(), ingestor.store.fact_row_count()), before,
        )

        print("\n9. Idempotent re-ingestion and path moves")
        again = ingestor.ingest_file(os.path.join(fixtures, "demo-A.xml"))
        checker.check("re-ingest outcome", again.outcome, "already_ingested")
        moved = os.path.join(workdir, "moved", "demo-A-renamed.xml")
        os.makedirs(os.path.dirname(moved))
        shutil.copy(os.path.join(fixtures, "demo-A.xml"), moved)
        moved_report = ingestor.ingest_file(moved)
        checker.check("moved file outcome", moved_report.outcome, "already_ingested")
        checker.check(
            "moved file shares source id", moved_report.source_id, reports["demo-A.xml"].source_id
        )
        checker.check(
            "fact count after moves",
            (ingestor.store.source_count(), ingestor.store.fact_row_count()), before,
        )

        print("\n10. Traceability and offline reads")
        trace = ingestor.trace_fact(reports["demo-A.xml"].source_id, 1)
        checker.check("trace concept", trace.concept, X.REVENUE)
        checker.check("trace value", trace.value_text, "1234.50")
        checker.check("trace context id in source", trace.context_id_in_source, "c-alpha")
        checker.check("trace unit id in source", trace.unit_id_in_source, "u-money")
        checker.check("trace line", trace.location.line > 1, True)
        snapshot = [
            (r.coordinate.canonical_text(), r.status,
             [f.value_text for f in r.candidates])
            for r in ingestor.query()
        ]
        ingestor.close()
        for name in os.listdir(fixtures):
            if name.endswith(".xml"):
                os.remove(os.path.join(fixtures, name))
        for name in os.listdir(fixtures):
            if name.endswith(".json"):
                os.remove(os.path.join(fixtures, name))
        reopened = XbrlIngestor(db_path, dictionary_copy)
        try:
            after = [
                (r.coordinate.canonical_text(), r.status,
                 [f.value_text for f in r.candidates])
                for r in reopened.query()
            ]
            checker.check("offline query identical to online", after, snapshot)
            offline_trace = reopened.trace_fact(reports["demo-A.xml"].source_id, 1)
            checker.check("offline trace value", offline_trace.value_text, "1234.50")
        finally:
            reopened.close()

        print("\n11. Real process kill mid-ingest, then retry")
        crash_db = os.path.join(workdir, "crash.sqlite")
        XbrlStore(crash_db).close()
        child = os.path.join(workdir, "crash_child.py")
        with open(child, "w", encoding="utf-8") as fh:
            fh.write(
                "import os, sys\n"
                "sys.path.insert(0, %r)\n"
                "from xbrl_ingest import XbrlIngestor\n"
                "def hook(source_id):\n"
                "    print('WROTE', source_id, flush=True)\n"
                "    os._exit(77)\n"
                "XbrlIngestor(sys.argv[1], sys.argv[2]).ingest_file(\n"
                "    sys.argv[3], system_exit_hook=hook)\n"
                "print('COMMITTED')\n" % PROJECT
            )
        # Re-create the copied fixtures (deleted above) for this child.
        shutil.copy(os.path.join(FIXTURES, "demo-A.xml"),
                    os.path.join(workdir, "crash-A.xml"))
        shutil.copy(os.path.join(FIXTURES, "concepts.json"),
                    os.path.join(workdir, "crash-concepts.json"))
        proc = subprocess.run(
            [sys.executable, "-I", child, crash_db,
             os.path.join(workdir, "crash-concepts.json"),
             os.path.join(workdir, "crash-A.xml")],
            capture_output=True, text=True,
        )
        checker.check("child exit code", proc.returncode, 77)
        checker.check("child reached write hook", "WROTE" in proc.stdout, True)
        checker.check("child never committed", "COMMITTED" in proc.stdout, False)
        store = XbrlStore(crash_db)
        checker.check("sources visible after kill", store.source_count(), 0)
        checker.check("facts visible after kill", store.fact_row_count(), 0)
        store.close()
        retry = XbrlIngestor(crash_db, os.path.join(workdir, "crash-concepts.json"))
        report = retry.ingest_file(os.path.join(workdir, "crash-A.xml"))
        checker.check("retry completes", report.fact_count, X.DEMO_A["fact_count"])
        checker.check("retry source count", retry.store.source_count(), 1)
        checker.check("retry fact count", retry.store.fact_row_count(), X.DEMO_A["fact_count"])
        retry.close()

        print("\n12. Same path, new bytes -> new source")
        sliding = os.path.join(workdir, "sliding.xml")
        shutil.copy(os.path.join(FIXTURES, "demo-C.xml"), sliding)
        fresh_db = os.path.join(workdir, "sliding.sqlite")
        fresh = XbrlIngestor(fresh_db, os.path.join(FIXTURES, "concepts.json"))
        first = fresh.ingest_file(sliding)
        shutil.copy(os.path.join(FIXTURES, "demo-D.xml"), sliding)
        second = fresh.ingest_file(sliding)
        checker.check("new content is a new source", first.source_id != second.source_id, True)
        checker.check("same-path new content outcome", second.outcome,
                      "new-source-same-path")
        checker.check("both sources retained", fresh.store.source_count(), 2)
        checker.check(
            "facts from both sources retained",
            fresh.store.fact_row_count(), X.DEMO_C["fact_count"] + X.DEMO_D["fact_count"],
        )
        fresh.close()

        return checker.finish()
    finally:
        shutil.rmtree(workdir, True)


if __name__ == "__main__":
    raise SystemExit(main())
