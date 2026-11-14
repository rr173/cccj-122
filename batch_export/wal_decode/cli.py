"""Command line entry point for the offline WAL change decoder.

Two subcommands:

* ``build-manifest`` -- collection-side helper: hash a baseline database and a
  raw WAL and write the collection manifest (lengths, SHA-256, ordered
  identity columns);
* ``decode`` -- run the offline decoder.

Exit status (decode):

* 0  -- complete parse (an empty WAL or no-change commits are still success);
* 2  -- usage / input validation error (no output file is produced);
* 3  -- WAL corruption or unsupported input;
* 10 -- parse finished with an uncommitted/partial tail: every committed
        transaction is emitted, but the output is explicitly marked
        incomplete and is never written as a normal full result.

Output is a directory containing ``commits.jsonl`` (one JSON object per
committed transaction, in commit order) plus ``status.json`` (outcome,
fingerprints, tail diagnosis). A stream-oriented ``--stdout`` mode prints the
same commit objects without creating an output directory.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .decoder import WalChangeDecoder
from .errors import (
    InputValidationError,
    Outcome,
    WalDecodeError,
    WalFormatError,
    WalUnsupportedError,
)
from .manifest import (
    TrackedTable,
    build_manifest,
    load_manifest,
)
from .values import dumps_json

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CORRUPT_OR_UNSUPPORTED = 3
EXIT_INCOMPLETE = 10


def _parse_table_spec(spec: str) -> TrackedTable:
    if "=" not in spec:
        raise InputValidationError(
            f"bad --table value {spec!r}; expected NAME=col1,col2,..."
        )
    name, _, cols = spec.partition("=")
    columns = tuple(c.strip() for c in cols.split(",") if c.strip())
    return TrackedTable(name.strip(), columns)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m wal_decode.cli",
        description="Decode per-transaction net row changes from a raw SQLite WAL.",
    )
    sub = parser.add_subparsers(dest="command")

    dec = sub.add_parser("decode", help="decode a baseline + raw WAL pair")
    dec.add_argument("--manifest", help="collection manifest JSON")
    dec.add_argument("--db", help="baseline main database (if not in manifest)")
    dec.add_argument("--wal", help="raw WAL file (if not in manifest)")
    dec.add_argument(
        "--table", action="append", default=[],
        help="tracked table as NAME=identity_col1,identity_col2 (repeatable)",
    )
    target = dec.add_mutually_exclusive_group()
    target.add_argument(
        "--out",
        help="output directory for commits.jsonl + status.json",
    )
    target.add_argument(
        "--stdout", action="store_true",
        help="print commit objects as JSONL instead of writing an output directory",
    )

    man = sub.add_parser(
        "build-manifest",
        help="hash a baseline + WAL pair and write a collection manifest",
    )
    man.add_argument("--db", required=True, help="baseline main database")
    man.add_argument("--wal", required=True, help="raw WAL file")
    man.add_argument(
        "--table", action="append", required=True,
        help="tracked table as NAME=identity_col1,identity_col2 (repeatable)",
    )
    man.add_argument("--output", required=True, help="manifest JSON path")
    man.add_argument("--note", default="", help="free-text collection note")
    return parser


def _resolve_inputs(args) -> tuple[str, str, tuple[TrackedTable, ...]]:
    if args.manifest:
        manifest = load_manifest(args.manifest)
        db_path = args.db or manifest.database.path
        wal_path = args.wal or manifest.wal.path
        tables = manifest.tables
        if args.table:
            raise InputValidationError(
                "--table cannot be combined with --manifest (the manifest owns the "
                "tracked identity definitions)"
            )
    else:
        if not args.db or not args.wal:
            raise InputValidationError(
                "without --manifest both --db and --wal are required"
            )
        if not args.table:
            raise InputValidationError(
                "at least one --table NAME=identity_col1,... is required"
            )
        db_path, wal_path = args.db, args.wal
        tables = tuple(_parse_table_spec(spec) for spec in args.table)
    if not args.stdout and not args.out:
        raise InputValidationError("choose --out DIR or --stdout")
    return db_path, wal_path, tables


def _status_payload(decoder: WalChangeDecoder) -> dict:
    payload = {
        "outcome": decoder.outcome.value,
        "commit_count": decoder.commit_count,
        "database": decoder._db_fingerprint.__dict__,
        "wal": decoder._wal_fingerprint.__dict__,
        "tracked_tables": [
            {"table": t.name, "identity_columns": list(t.identity_columns)}
            for t in decoder._tables
        ],
    }
    if decoder.wal_header is not None:
        h = decoder.wal_header
        payload["wal_header"] = {
            "magic": f"0x{h.magic:08x}",
            "format_version": h.format_version,
            "page_size": h.page_size,
            "checksum_byte_order": "big-endian" if h.big_endian_checksums
            else "little-endian",
            "salt1": f"0x{h.salt1:08x}",
            "salt2": f"0x{h.salt2:08x}",
        }
    else:
        payload["wal_header"] = None
        payload["note"] = "WAL file is empty (zero bytes): no frames, no transactions"
    if decoder.tail is not None:
        payload["tail"] = decoder.tail.describe()
        payload["tail_fields"] = decoder.tail.__dict__
    return payload


def _run_build_manifest(args) -> int:
    tables = tuple(_parse_table_spec(spec) for spec in args.table)
    manifest = build_manifest(args.db, args.wal, tables, note=args.note)
    parent = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(parent, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(manifest.to_dict(), fh, ensure_ascii=False, indent=2)
    print(f"wrote manifest {args.output}")
    print(f"  db  {manifest.database.byte_length} bytes sha256={manifest.database.sha256}")
    print(f"  wal {manifest.wal.byte_length} bytes sha256={manifest.wal.sha256}")
    return EXIT_OK


def _run_decode(args) -> int:
    db_path, wal_path, tables = _resolve_inputs(args)

    out_dir = None
    commits_path = None
    status_path = None
    tmp_commits = tmp_status = None
    if args.out:
        out_dir = os.path.abspath(args.out)
        if os.path.exists(out_dir) and not os.path.isdir(out_dir):
            print(f"input error: --out path {out_dir} is not a directory",
                  file=sys.stderr)
            return EXIT_USAGE
        os.makedirs(out_dir, exist_ok=True)
        commits_path = os.path.join(out_dir, "commits.jsonl")
        status_path = os.path.join(out_dir, "status.json")
        # Write into temporary names first; a failed decode must never leave a
        # file that could be mistaken for a complete result.
        tmp_commits = commits_path + ".partial"
        tmp_status = status_path + ".partial"
        for stale in (tmp_commits, tmp_status):
            if os.path.exists(stale):
                os.remove(stale)

    commit_records: list[dict] = []
    try:
        with WalChangeDecoder(db_path, wal_path, tables) as decoder:
            if args.stdout:
                for commit in decoder.iter_commits():
                    print(dumps_json(commit.to_dict()))
                status = _status_payload(decoder)
                print(dumps_json({"status": status}), file=sys.stderr)
                outcome = decoder.outcome
                tail = decoder.tail
            else:
                with open(tmp_commits, "w", encoding="utf-8") as out_fh:
                    for commit in decoder.iter_commits():
                        record = commit.to_dict()
                        commit_records.append(record)
                        out_fh.write(dumps_json(record) + "\n")
                status = _status_payload(decoder)
                with open(tmp_status, "w", encoding="utf-8") as sfh:
                    sfh.write(json.dumps(status, ensure_ascii=False, indent=2))
                outcome = decoder.outcome
                tail = decoder.tail
    except (WalFormatError, WalUnsupportedError, WalDecodeError) as exc:
        print(f"decode failed: {exc.detailed()}", file=sys.stderr)
        if out_dir:
            for partial in (tmp_commits, tmp_status):
                if partial and os.path.exists(partial):
                    os.remove(partial)
        return EXIT_CORRUPT_OR_UNSUPPORTED
    except InputValidationError as exc:
        print(f"input error: {exc.detailed()}", file=sys.stderr)
        if out_dir:
            for partial in (tmp_commits, tmp_status):
                if partial and os.path.exists(partial):
                    os.remove(partial)
        return EXIT_USAGE

    if args.out:
        os.replace(tmp_commits, commits_path)
        os.replace(tmp_status, status_path)
        if outcome is Outcome.INCOMPLETE:
            marker = os.path.join(out_dir, "INCOMPLETE.txt")
            with open(marker, "w", encoding="utf-8") as mf:
                mf.write("INCOMPLETE DECODE: committed transactions only\n")
                mf.write(tail.describe() + "\n")
        print(f"wrote {len(commit_records)} commit record(s) to {commits_path}")
        print(f"status: {outcome.value} -> {status_path}")
    if outcome is Outcome.INCOMPLETE:
        print(f"incomplete: {tail.describe()}", file=sys.stderr)
        return EXIT_INCOMPLETE
    return EXIT_OK


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        if args.command == "build-manifest":
            return _run_build_manifest(args)
        return _run_decode(args)
    except InputValidationError as exc:
        print(f"input error: {exc.detailed()}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(run())
