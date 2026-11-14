"""Terminal commands for the local XBRL ingester.

    python3 -m xbrl_ingest.cli ingest DB --dict DICT.json FILE...
    python3 -m xbrl_ingest.cli query  DB --dict DICT.json [filters]
    python3 -m xbrl_ingest.cli trace  DB --dict DICT.json --source SOURCE --position N
    python3 -m xbrl_ingest.cli show   DB --dict DICT.json --digest FACT_DIGEST
    python3 -m xbrl_ingest.cli sources DB --dict DICT.json
    python3 -m xbrl_ingest.cli context DB --dict DICT.json SOURCE_ID
    python3 -m xbrl_ingest.cli unit    DB --dict DICT.json SOURCE_ID

Every command exits 0 on success; ingestion failures exit 2 after printing the
file and location of the problem and leave the warehouse untouched.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .dictionary import DictionaryError
from .errors import IngestError
from .models import QueryResult
from .service import XbrlIngestor


def _json_default(value):
    if hasattr(value, "canonical"):
        return value.canonical()
    raise TypeError(f"not JSON serialisable: {value!r}")


def coordinate_json(result: QueryResult) -> dict:
    coordinate = result.coordinate
    return {
        "concept": coordinate.concept,
        "entity": {
            "scheme": coordinate.context.entity.scheme,
            "identifier": coordinate.context.entity.identifier,
        },
        "period": {
            "kind": coordinate.context.period.kind,
            "instant": coordinate.context.period.instant,
            "start": coordinate.context.period.start,
            "end": coordinate.context.period.end,
        },
        "dimensions": [
            {"dimension": name, "member": member}
            for name, member in coordinate.context.dimensions
        ],
        "unit": coordinate.unit.canonical() if coordinate.unit else None,
        "language": coordinate.language,
    }


def result_json(result: QueryResult) -> dict:
    return {
        "status": result.status,
        "meaningfully_equal": result.meaningfully_equal,
        "coordinate": coordinate_json(result),
        "candidate_count": len(result.candidates),
        "sources": list(result.sources),
        "candidates": [fact_json(f) for f in result.candidates],
    }


def fact_json(fact) -> dict:
    return {
        "fact_id": fact.fact_id,
        "source_id": fact.source_id,
        "position": fact.position,
        "concept": fact.concept,
        "context_id_in_source": fact.context_id_in_source,
        "unit_id_in_source": fact.unit_id_in_source,
        "nil": fact.nil,
        "decimals": fact.decimals,
        "language": fact.language,
        "value_type": fact.value_type,
        "value_text": fact.value_text,
        "value_canonical": fact.value_canonical,
        "location": str(fact.location),
    }


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("db", help="SQLite warehouse path")
    parser.add_argument("--dict", dest="dictionary", required=True,
                        help="local concept dictionary JSON (never downloaded)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xbrl_ingest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="parse and atomically store instance files")
    _add_common(p)
    p.add_argument("files", nargs="+", help="XBRL instance files")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("query", help="query facts by concept/entity/period/dimension")
    _add_common(p)
    p.add_argument("--concept", help="full QName text {uri}local")
    p.add_argument("--concept-local")
    p.add_argument("--concept-namespace")
    p.add_argument("--scheme", dest="entity_scheme")
    p.add_argument("--identifier", dest="entity_identifier")
    p.add_argument("--period-kind", choices=("instant", "duration"))
    p.add_argument("--instant")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--dimension", action="append", default=[],
                   metavar="DIMENSION=MEMBER",
                   help="expanded dimension=member QName pair; repeatable")
    p.add_argument("--source", dest="source_id")
    p.add_argument("--source-path")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("search", help="find text facts by value")
    _add_common(p)
    p.add_argument("text")
    p.add_argument("--exact", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trace", help="trace one stored fact occurrence")
    _add_common(p)
    p.add_argument("--source", dest="source_id", required=True)
    p.add_argument("--position", type=int, required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="show all candidates for a fact coordinate digest")
    _add_common(p)
    p.add_argument("--digest", required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("sources", help="list ingested sources")
    _add_common(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("describe", help="JSON view of one source and its declarations")
    _add_common(p)
    p.add_argument("source_id")

    p = sub.add_parser("context", help="list contexts declared by a source")
    _add_common(p)
    p.add_argument("source_id")

    p = sub.add_parser("unit", help="list units declared by a source")
    _add_common(p)
    p.add_argument("source_id")
    return parser


def _parse_dimensions(raw: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"--dimension wants DIMENSION=MEMBER, got {item!r}")
        name, member = item.split("=", 1)
        if name.startswith("{"):
            pairs.append((name.strip(), member.strip()))
        else:
            raise SystemExit(
                f"dimension QNames must be expanded {namespace}local, got {name!r}"
            )
    return pairs


def _print(obj, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default))
    else:
        print(human)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ingestor = XbrlIngestor(args.db, args.dictionary)
    except DictionaryError as exc:
        print(f"dictionary error: {exc}", file=sys.stderr)
        return 2
    with ingestor:
        if args.command == "ingest":
            return _cmd_ingest(ingestor, args)
        if args.command == "query":
            return _cmd_query(ingestor, args)
        if args.command == "search":
            return _cmd_search(ingestor, args)
        if args.command == "trace":
            return _cmd_trace(ingestor, args)
        if args.command == "show":
            return _cmd_show(ingestor, args)
        if args.command == "sources":
            return _cmd_sources(ingestor, args)
        if args.command == "describe":
            return _cmd_describe(ingestor, args)
        if args.command in ("context", "unit"):
            return _cmd_declarations(ingestor, args)
    return 1  # pragma: no cover


def _cmd_ingest(ingestor: XbrlIngestor, args) -> int:
    failed = 0
    for path in args.files:
        try:
            report = ingestor.ingest_file(path)
        except IngestError as exc:
            failed += 1
            print(f"REFUSED {path}: {exc}", file=sys.stderr)
            continue
        except OSError as exc:
            failed += 1
            print(f"REFUSED {path}: cannot read file: {exc}", file=sys.stderr)
            continue
        if args.json:
            print(json.dumps({
                "source_id": report.source_id,
                "content_sha256": report.content_sha256,
                "path": report.path,
                "run_id": report.run_id,
                "outcome": report.outcome,
                "fact_count": report.fact_count,
                "context_count": report.context_count,
                "unit_count": report.unit_count,
                "warnings": list(report.warnings),
            }, ensure_ascii=False, indent=2))
        else:
            print(
                f"{report.outcome}: {report.path} -> {report.source_id}"
                f" ({report.fact_count} facts, {report.context_count} contexts,"
                f" {report.unit_count} units)"
            )
            for warning in report.warnings:
                print(f"  warning: {warning}")
    return 2 if failed else 0


def _cmd_query(ingestor: XbrlIngestor, args) -> int:
    results = ingestor.query(
        concept=args.concept,
        concept_local=args.concept_local,
        concept_namespace=args.concept_namespace,
        entity_scheme=args.entity_scheme,
        entity_identifier=args.entity_identifier,
        period_kind=args.period_kind,
        instant=args.instant,
        start=args.start,
        end=args.end,
        dimensions=_parse_dimensions(args.dimension),
        source_id=args.source_id,
        source_path=args.source_path,
    )
    payload = [result_json(r) for r in results]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("no facts match this filter")
        return 0
    for result in results:
        coordinate = coordinate_json(result)
        print(f"[{result.status.upper()}] {coordinate['concept']}")
        entity = coordinate["entity"]
        print(f"  entity: {entity['scheme']} {entity['identifier']!r}")
        period = coordinate["period"]
        if period["kind"] == "instant":
            print(f"  period: instant {period['instant']}")
        else:
            print(f"  period: duration {period['start']} .. {period['end']}")
        for dim in coordinate["dimensions"]:
            print(f"  dimension: {dim['dimension']} = {dim['member']}")
        if coordinate["unit"]:
            unit = coordinate["unit"]
            num = " * ".join(f"{m}" + (f"^{c}" if c > 1 else "")
                             for m, c in unit["numerator"])
            if unit["denominator"]:
                den = " * ".join(f"{m}" + (f"^{c}" if c > 1 else "")
                                 for m, c in unit["denominator"])
                print(f"  unit: {num} / {den}")
            else:
                print(f"  unit: {num}")
        if coordinate["language"]:
            print(f"  language: {coordinate['language']}")
        print(f"  candidates: {len(result.candidates)}")
        for fact in result.candidates:
            value = "nil" if fact.nil else repr(fact.value_text)
            print(f"    - {fact.source_id} pos {fact.position} value={value}"
                  f" decimals={fact.decimals} at {fact.location}")
    return 0


def _cmd_search(ingestor: XbrlIngestor, args) -> int:
    results = ingestor.search_text(args.text, exact=args.exact)
    payload = [result_json(r) for r in results]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("no text fact matches")
        return 0
    for result in results:
        print(f"[{result.status.upper()}] {result.coordinate.concept}"
              f" lang={result.coordinate.language}")
        for fact in result.candidates:
            print(f"    - {fact.source_id} pos {fact.position}"
                  f" value={fact.value_text!r} at {fact.location}")
    return 0


def _cmd_trace(ingestor: XbrlIngestor, args) -> int:
    fact = ingestor.trace_fact(args.source_id, args.position)
    if fact is None:
        print(f"no fact at {args.source_id} position {args.position}", file=sys.stderr)
        return 1
    payload = fact_json(fact)
    payload["coordinate"] = {
        "concept": fact.coordinate.concept,
        "canonical_text": fact.coordinate.canonical_text(),
        "digest": fact.coordinate.digest,
    }
    _print(payload, args.json, json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_show(ingestor: XbrlIngestor, args) -> int:
    result = ingestor.store.result_for_digest(args.digest)
    if result is None:
        print(f"no facts for coordinate digest {args.digest}", file=sys.stderr)
        return 1
    payload = result_json(result)
    payload["coordinate_digest"] = args.digest
    _print(payload, args.json, json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_sources(ingestor: XbrlIngestor, args) -> int:
    payload = [
        {
            "source_id": item.source.source_id,
            "content_sha256": item.source.content_sha256,
            "byte_size": item.source.byte_size,
            "path": item.path,
            "original_name": item.source.original_name,
            "first_run_id": item.source.first_run_id,
            "ingested_at": item.source.ingested_at,
        }
        for item in ingestor.sources()
    ]
    _print(payload, args.json, json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_describe(ingestor: XbrlIngestor, args) -> int:
    try:
        payload = ingestor.describe_source(args.source_id)
    except KeyError:
        print(f"unknown source {args.source_id}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_declarations(ingestor: XbrlIngestor, args) -> int:
    if args.command == "context":
        declarations = ingestor.contexts_for_source(args.source_id)
    else:
        declarations = ingestor.units_for_source(args.source_id)
    if not declarations:
        print(f"source {args.source_id} has no {args.command} declarations",
              file=sys.stderr)
        return 1
    payload = []
    for declaration_id, coordinate, location in declarations:
        payload.append({
            "id_in_source": declaration_id,
            "coordinate": coordinate.canonical(),
            "digest": coordinate.digest,
            "location": str(location),
        })
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
