"""Terminal commands for calculation relationship checks.

    python3 -m calc_check.cli networks LINKBASE --mapping MAP.json --dict DICT.json
    python3 -m calc_check.cli check DB LINKBASE --mapping MAP.json \\
        --dict DICT.json --source SRC1 --source SRC2 [--report out.json]

Exit statuses (designed for automation):

* 0  success: the report contains no ``inconsistent`` coordinate;
* 2  invocation error (unknown source id, unreadable warehouse/dictionary);
* 3  relationship definition error (invalid linkbase or mapping: nothing ran);
* 4  the definition is valid and the check ran, but at least one coordinate is
  ``inconsistent``; ``indeterminate`` alone keeps exit 0 unless
  ``--fail-on-indeterminate`` is given.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .errors import (
    EXIT_DEFINITION,
    EXIT_INCONSISTENT,
    EXIT_INVOCATION,
    CalcCheckError,
)
from .service import CalculationChecker


def _dump(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calc_check", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "networks",
        help="parse and resolve the linkbase; show effective edges and arc fates",
    )
    p.add_argument("linkbase")
    p.add_argument("--mapping", required=True, help="explicit #fragment -> concept JSON")
    p.add_argument("--dict", dest="dictionary", required=True,
                   help="local concept dictionary JSON (never downloaded)")

    p = sub.add_parser("check", help="check selected sources against the linkbase")
    p.add_argument("db", help="existing SQLite fact warehouse (opened read-only)")
    p.add_argument("linkbase")
    p.add_argument("--mapping", required=True)
    p.add_argument("--dict", dest="dictionary", required=True)
    p.add_argument("--source", dest="sources", action="append", required=True,
                   help="source id to include; repeat to select several")
    p.add_argument("--report", help="write the machine-readable JSON report here")
    p.add_argument("--fail-on-indeterminate", action="store_true",
                   help="also exit non-zero (5) when any coordinate is indeterminate")
    return parser


def _print_human_check(report: dict) -> None:
    counts = report["counts"]
    print(f"warehouse: {report['warehouse']}")
    print(f"sources: {', '.join(report['selected_sources'])}")
    print("verdict counts: "
          f"consistent={counts['consistent']} "
          f"inconsistent={counts['inconsistent']} "
          f"indeterminate={counts['indeterminate']}")
    for result in report["checks"]:
        print(f"\n[{result['verdict'].upper()}] role={result['role']}")
        print(f"  total: {result['total']}")
        coord = result["coordinate"]
        entity = coord["entity"]
        print(f"  entity: {entity['scheme']} {entity['identifier']!r}")
        period = coord["period"]
        if period["kind"] == "instant":
            print(f"  period: instant {period['instant']}")
        else:
            print(f"  period: duration {period['start']} .. {period['end']}")
        for dim in coord["dimensions"]:
            print(f"  dimension: {dim[0]} = {dim[1]}")
        unit = coord["unit"]
        if unit:
            num = " * ".join(f"{m} x{c}" if c > 1 else m for m, c in unit["numerator"])
            den = " * ".join(f"{m} x{c}" if c > 1 else m for m, c in unit["denominator"])
            print(f"  unit: {num}" + (f" / {den}" if den else ""))
        for edge in result["edges"]:
            print(f"  edge: {edge['item']} weight={edge['weight']}")
        if result.get("total_interval"):
            print(f"  total interval: {result['total_interval']}")
        if result.get("sum_interval"):
            print(f"  weighted sum interval: {result['sum_interval']}")
        if result["verdict"] == "indeterminate":
            for reason in result["reasons"]:
                print(f"  reason: {reason}")
        else:
            print(f"  explanation: {result['explanation']}")
        for entry in result.get("weighted_items", []):
            print(f"    item {entry['edge']['item']}: {entry['source_interval']} * "
                  f"{entry['weight']} -> {entry['weighted_interval']} "
                  f"({entry['candidate_count']} candidate fact(s))")
    for item in report["unchecked"]:
        print(f"\n[UNCHECKED] role={item['role']} total={item['total']}")
        print(f"  reason: {item['reason']}")
    print(f"\n{report['precision_notice']}")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        checker = CalculationChecker(args.linkbase, args.mapping, args.dictionary)
    except CalcCheckError as exc:
        print(f"definition failure: {exc}", file=sys.stderr)
        return EXIT_DEFINITION
    except FileNotFoundError as exc:
        print(f"invocation error: {exc}", file=sys.stderr)
        return EXIT_INVOCATION

    if args.command == "networks":
        print(_dump(checker.validate_definition()))
        return 0

    try:
        report = checker.check_sources(args.db, args.sources)
    except CalcCheckError as exc:
        if exc.exit_code == EXIT_DEFINITION:
            print(f"definition failure: {exc}", file=sys.stderr)
        else:
            print(f"invocation error: {exc}", file=sys.stderr)
        return exc.exit_code

    payload = _dump(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        print(f"report written to {args.report}")
    _print_human_check(report)

    if report["counts"]["inconsistent"]:
        return EXIT_INCONSISTENT
    if args.fail_on_indeterminate and report["counts"]["indeterminate"]:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
