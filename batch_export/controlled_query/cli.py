"""Command line for controlled analytical queries.

    python3 -m controlled_query.cli run DB --catalog C.json --policy P.json \
        --identity alice (--sql-file Q.sql | --sql "SELECT ...") \
        [--result OUT.json] [--overwrite] [--json]
    python3 -m controlled_query.cli check-config DB --catalog C.json --policy P.json

Exit codes: 0 success, 2 invocation error (bad arguments, missing files,
unknown identity), 3 catalog/policy/data-contract error, 4 SQL outside the
approved subset, 5 the approved statement failed to execute.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .errors import (
    EXIT_CONFIG,
    EXIT_EXECUTION,
    EXIT_INVOCATION,
    EXIT_UNSUPPORTED,
    ConfigError,
    ControlledQueryError,
    InvocationError,
    QueryExecutionError,
    UnsupportedQueryError,
)
from .service import ControlledQueryService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="controlled_query",
        description="Controlled analytical queries over a SQLite warehouse:"
        " each caller sees only the rows and fields their identity is"
        " authorized for.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run one approved SELECT for one identity")
    p.add_argument("db", help="existing SQLite warehouse (opened read-only)")
    p.add_argument("--catalog", required=True, help="relation catalog JSON")
    p.add_argument("--policy", required=True, help="authorization policy JSON")
    p.add_argument("--identity", required=True,
                   help="registered identity the query runs as")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--sql-file", help="file with exactly one SELECT")
    source.add_argument("--sql", help="the SELECT text itself")
    p.add_argument("--result", help="write the machine-readable JSON result"
                   " here (refuses to overwrite)")
    p.add_argument("--overwrite", action="store_true",
                   help="allow replacing an existing result file")
    p.add_argument("--json", action="store_true",
                   help="print the full result document to stdout")

    p = sub.add_parser("check-config",
                       help="validate catalog + policy + live schema +"
                       " target keys without running a user query")
    p.add_argument("db")
    p.add_argument("--catalog", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "check-config":
            return _cmd_check_config(args)
    except UnsupportedQueryError as exc:
        print(f"unsupported SQL: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except QueryExecutionError as exc:
        print(f"query failed: {exc}", file=sys.stderr)
        return EXIT_EXECUTION
    except InvocationError as exc:
        print(f"invocation error: {exc}", file=sys.stderr)
        return EXIT_INVOCATION
    except ControlledQueryError as exc:  # pragma: no cover - fallback
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_EXECUTION
    return EXIT_INVOCATION  # pragma: no cover


def _cmd_run(args) -> int:
    service = ControlledQueryService(args.db, args.catalog, args.policy)
    report = service.query(
        args.identity,
        sql=args.sql,
        sql_file=args.sql_file,
        result_path=args.result,
        overwrite=args.overwrite,
    )
    if args.json:
        print(json.dumps(report.to_document(), ensure_ascii=False, indent=2))
        return 0
    print(f"identity  : {report.identity}")
    print(f"relations : {', '.join(report.relations) or '(none)'}")
    print(f"columns   : {len(report.columns)}")
    print(f"rows      : {len(report.rows)}")
    if report.result_path:
        print(f"result    : {report.result_path}")
    return 0


def _cmd_check_config(args) -> int:
    service = ControlledQueryService(args.db, args.catalog, args.policy)
    report = service.check_config()
    payload = [
        {
            "name": name,
            "source": source,
            "fields": fields,
            "owner_path": [
                {"table": table, "left": left, "right": right}
                for table, left, right in hops
            ],
            "summary_hidden": hidden,
        }
        for name, source, fields, hops, hidden in report.relations
    ]
    if args.json:
        print(json.dumps({"relations": payload}, ensure_ascii=False, indent=2))
    else:
        for item in payload:
            print(f"relation {item['name']} <- {item['source']}")
            print(f"  fields         : {', '.join(item['fields'])}")
            for hop in item["owner_path"]:
                print(f"  hop -> {hop['table']}"
                      f" on {hop['left']} = {hop['right']}")
            print(f"  summary hidden : {', '.join(item['summary_hidden'])}")
        print("configuration OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
