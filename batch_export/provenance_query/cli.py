"""Command line for provenance-carrying queries.

    python3 -m provenance_query.cli build  DB SQL_FILE RESULT_FILE
    python3 -m provenance_query.cli read   RESULT_FILE [--json] [--identities] [--graph]
    python3 -m provenance_query.cli eval   RESULT_FILE --exclude IDENTITY [--exclude ...]
    python3 -m provenance_query.cli verify RESULT_FILE

Exit codes: 0 success, 2 invocation/argument error, 3 unsupported SQL,
4 build error, 5 result file missing or unreadable.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .errors import (
    BuildError,
    EXIT_BUILD,
    EXIT_INVOCATION,
    EXIT_UNSUPPORTED,
    InvocationError,
    ProvenanceQueryError,
    UnsupportedQueryError,
)
from .reader import (
    ExclusionError,
    ProvenanceResultReader,
    evaluate_exclusion,
)
from .service import ProvenanceQueryBuilder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provenance_query",
        description="Provenance-carrying relational queries over a SQLite warehouse.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="run one SELECT and write a result file")
    p.add_argument("db", help="existing SQLite database (opened read-only)")
    p.add_argument("sql_file", help="file containing exactly one SELECT statement")
    p.add_argument("result_file", help="result file to create (refuses to overwrite)")
    p.add_argument("--overwrite", action="store_true",
                   help="replace an existing result file")
    p.add_argument("--json", action="store_true", help="machine-readable summary")

    p = sub.add_parser("read", help="read the compressed results and graph")
    p.add_argument("result_file")
    p.add_argument("--json", action="store_true", help="raw compressed JSON")
    p.add_argument("--identities", action="store_true",
                   help="list the base-row identities in the snapshot")
    p.add_argument("--graph", action="store_true",
                   help="show the graph node/edge table summary")

    p = sub.add_parser("eval", help="re-evaluate multiplicities after exclusions")
    p.add_argument("result_file")
    p.add_argument("--exclude", action="append", default=[], metavar="IDENTITY",
                   help='base-row identity, e.g. "a(id=1)"; repeatable')
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("verify", help="check stored multiplicities against the graph")
    p.add_argument("result_file")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            return _cmd_build(args)
        if args.command == "read":
            return _cmd_read(args)
        if args.command == "eval":
            return _cmd_eval(args)
        if args.command == "verify":
            return _cmd_verify(args)
    except UnsupportedQueryError as exc:
        print(f"unsupported SQL: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except ExclusionError as exc:
        print(f"exclusion error: {exc}", file=sys.stderr)
        return EXIT_INVOCATION
    except InvocationError as exc:
        print(f"invocation error: {exc}", file=sys.stderr)
        return EXIT_INVOCATION
    except BuildError as exc:
        print(f"build error: {exc}", file=sys.stderr)
        return EXIT_BUILD
    except ProvenanceQueryError as exc:  # pragma: no cover - fallback
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BUILD
    return EXIT_INVOCATION  # pragma: no cover


def _cmd_build(args) -> int:
    builder = ProvenanceQueryBuilder(args.db)
    report = builder.build(args.sql_file, args.result_file, overwrite=args.overwrite)
    payload = {
        "result_file": report.result_path,
        "result_rows": report.result_rows,
        "original_multiplicity_total": str(report.original_multiplicity_total),
        "graph_nodes": report.graph_nodes,
        "graph_edges": report.graph_edges,
        "tokens": report.tokens,
        "value_columns": report.value_columns,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"built {report.result_path}")
        print(f"  distinct result tuples : {report.result_rows}")
        print(f"  original multiplicities: {report.original_multiplicity_total}")
        print(f"  graph nodes / edges    : {report.graph_nodes} / {report.graph_edges}")
        print(f"  base-row tokens        : {report.tokens}")
    return 0


def _cmd_read(args) -> int:
    read = ProvenanceResultReader(args.result_file)
    try:
        if args.identities:
            payload = [
                {
                    "token": identity.token,
                    "table": identity.table,
                    "pk_columns": list(identity.pk_columns),
                    "pk_values": [v.encode() for v in identity.pk_values],
                    "identity": identity.describe(),
                }
                for identity in read.base_identities()
            ]
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        if args.graph:
            meta = read.meta()
            payload = {
                "graph_nodes": meta.get("graph_nodes"),
                "graph_edges": meta.get("graph_edges"),
                "tokens": meta.get("tokens"),
                "roots": [
                    {"ordinal": row.ordinal, "graph_root": row.graph_root}
                    for row in read.rows()
                ],
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        payload = read.raw_compressed()
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            for column in payload["columns"]:
                print(f"column {column['ordinal']}: {column['name']}")
            for row in payload["rows"]:
                print(f"row {row['ordinal']}: multiplicity {row['multiplicity']}")
                for column, value in zip(payload["columns"], row["values"]):
                    print(f"    {column['name']} = {_display(value)}")
        return 0
    finally:
        read.close()


def _display(encoded: dict) -> str:
    from .values import TyValue

    return TyValue.decode(encoded).display()


def _cmd_eval(args) -> int:
    read = ProvenanceResultReader(args.result_file)
    try:
        outcome = evaluate_exclusion(
            args.result_file, args.exclude, reader=read
        )
    finally:
        read.close()
    if args.json:
        print(json.dumps(outcome, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"excluded {outcome['excluded_count']} base row(s):")
        for identity in outcome["excluded"]:
            print(f"  {identity}")
        print(f"  rows changed: {outcome['rows_changed']}")
        print(f"  total before: {outcome['total_before']}")
        print(f"  total after : {outcome['total_after']}")
        for row in outcome["rows"]:
            print(f"  row {row['ordinal']}: {row['before']} -> {row['after']}")
    return 0


def _cmd_verify(args) -> int:
    read = ProvenanceResultReader(args.result_file)
    try:
        problems = read.validate()
        meta = read.meta()
    finally:
        read.close()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return EXIT_BUILD
    print(
        "result file verified:"
        f" {meta.get('result_rows')} tuples,"
        f" {meta.get('graph_nodes')} nodes,"
        f" {meta.get('tokens')} tokens"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
