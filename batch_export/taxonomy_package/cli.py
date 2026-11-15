"""Terminal commands for the offline taxonomy package registry.

    python3 -m taxonomy_package.cli init     REGISTRY
    python3 -m taxonomy_package.cli register REGISTRY PACKAGE.zip [PACKAGE.zip ...]
    python3 -m taxonomy_package.cli list     REGISTRY [--json]
    python3 -m taxonomy_package.cli show     REGISTRY --digest D [--json]
    python3 -m taxonomy_package.cli resolve  REGISTRY --digest D [--entry URI ...]
                                              [--batch N] [--json]
    python3 -m taxonomy_package.cli verify   REGISTRY [--json]
    python3 -m taxonomy_package.cli query    REGISTRY --digest D --uri URI [--json]

Exit codes: 0 success, 2 invocation error, 3 registry corruption,
4 malformed package / failed resolution, 5 atomic publish failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .errors import InvocationError, TaxonomyError
from .service import TaxonomyRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taxonomy_package",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create an empty registry directory")
    p.add_argument("registry")

    p = sub.add_parser("register", help="register taxonomy package ZIP(s)")
    p.add_argument("registry")
    p.add_argument("zips", nargs="+")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="list registered packages")
    p.add_argument("registry")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="package metadata, entries, rules, members")
    p.add_argument("registry")
    p.add_argument("--digest", required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("resolve",
                       help="resolve entry schema(s) to their dependency closure")
    p.add_argument("registry")
    p.add_argument("--digest", required=True)
    p.add_argument("--entry", action="append", default=[],
                   help="declared entry logical URI; repeatable (default: all)")
    p.add_argument("--batch", type=int,
                   help="print an already-published resolution batch instead")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("verify", help="offline verification of the whole registry")
    p.add_argument("registry")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("query", help="query public URI / href / final target")
    p.add_argument("registry")
    p.add_argument("--digest", required=True)
    p.add_argument("--uri", required=True)
    p.add_argument("--json", action="store_true")
    return parser


def _emit(payload, as_json: bool, human) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        human()


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            return _cmd_init(args)
        registry = TaxonomyRegistry(
            args.registry, create=(args.command == "register"))
        if args.command == "register":
            return _cmd_register(registry, args)
        if args.command == "list":
            return _cmd_list(registry, args)
        if args.command == "show":
            return _cmd_show(registry, args)
        if args.command == "resolve":
            return _cmd_resolve(registry, args)
        if args.command == "verify":
            return _cmd_verify(registry, args)
        if args.command == "query":
            return _cmd_query(registry, args)
    except TaxonomyError as exc:
        print(f"{exc.category} error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 1  # pragma: no cover


def _cmd_init(args) -> int:
    TaxonomyRegistry(args.registry, create=True)
    print(f"initialised empty registry at {args.registry}")
    return 0


def _cmd_register(registry: TaxonomyRegistry, args) -> int:
    payload = []
    for zip_path in args.zips:
        result = registry.register_zip(zip_path)
        payload.append({
            "zip": zip_path,
            "package_sha256": result.package_sha256,
            "identifier": result.identifier,
            "version": result.version,
            "reused": result.reused,
            "package_dir": result.package_dir,
            "member_count": result.member_count,
            "entry_count": result.entry_count,
        })

    def human():
        for item in payload:
            verb = "already registered (reused, no files copied)" \
                if item["reused"] else "registered"
            print(f"{verb}: {item['identifier']} {item['version']}"
                  f" ({item['package_sha256']})")
            print(f"  members: {item['member_count']},"
                  f" entries: {item['entry_count']}")
            print(f"  package dir: {item['package_dir']}")
    _emit(payload, args.json, human)
    return 0


def _cmd_list(registry: TaxonomyRegistry, args) -> int:
    packages = registry.list_packages()

    def human():
        if not packages:
            print("no packages registered")
            return
        for meta in packages:
            print(f"{meta['package_sha256']}  {meta['identifier']} {meta['version']}"
                  f"  ({meta['member_count']} members,"
                  f" {meta['entry_count']} entries)")
    _emit(packages, args.json, human)
    return 0


def _cmd_show(registry: TaxonomyRegistry, args) -> int:
    detail = registry.show_package(args.digest)
    payload = {
        "meta": detail["meta"],
        "entries": detail["entries"],
        "catalog_rules": detail["catalog_rules"],
        "members": detail["members"],
        "batches": detail["batches"],
    }

    def human():
        meta = detail["meta"]
        print(f"package {meta['package_sha256']}")
        print(f"  identifier: {meta['identifier']}")
        print(f"  name:       {meta['name']}")
        print(f"  version:    {meta['version']}")
        print(f"  members:    {meta['member_count']}")
        print("  entry points:")
        for row in detail["entries"]:
            print(f"    #{row['entry_seq']} {row['logical_uri']}"
                  f"  <- href {row['href']!r} (line {row['line']})")
        print("  catalog rewriteURI rules:")
        for row in detail["catalog_rules"]:
            print(f"    {row['uri_start_string']}  ->  {row['rewrite_prefix']}")
        print(f"  members ({len(detail['members'])}):")
        for row in detail["members"]:
            kind = "xml" if row["is_xml"] else "data"
            print(f"    [{kind}] {row['member_path']}"
                  f"  {row['blob_digest'][:16]}... ({row['byte_size']} bytes)")
        if detail["batches"]:
            print("  resolution batches:")
            for row in detail["batches"]:
                print(f"    batch {row['batch_id']}: {row['file_count']} files,"
                      f" {row['edge_count']} edges,"
                      f" {row['cycle_edge_count']} cycle edge(s)")
    _emit(payload, args.json, human)
    return 0


def _batch_json(batch) -> dict:
    return {
        "batch_id": batch.batch_id,
        "entry_uris": list(batch.entry_uris),
        "entry_member_paths": list(batch.entry_member_paths),
        "file_count": batch.file_count,
        "edge_count": batch.edge_count,
        "cycle_edge_count": batch.cycle_edge_count,
        "files": [vars(f) for f in batch.files],
        "edges": [vars(e) for e in batch.edges],
    }


def _cmd_resolve(registry: TaxonomyRegistry, args) -> int:
    if args.batch is not None:
        # A batch id the user typed but which was never published is an
        # invocation problem (exit 2); the registry itself is not corrupt.
        known = {row["batch_id"]
                 for row in registry.show_package(args.digest)["batches"]}
        if args.batch not in known:
            raise InvocationError(
                f"package {args.digest[:12]} has no resolution batch"
                f" {args.batch}; known batches: {sorted(known) or '(none)'}")
        batch = registry.load_batch(args.digest, args.batch)
    else:
        entries = tuple(args.entry) if args.entry else None
        batch = registry.resolve_entries(args.digest, entries)
    payload = _batch_json(batch)

    def human():
        print(f"batch {batch.batch_id}: {batch.file_count} file(s),"
              f" {batch.edge_count} dependency edge(s),"
              f" {batch.cycle_edge_count} cycle edge(s)")
        print("entries:")
        for uri, path in zip(batch.entry_uris, batch.entry_member_paths):
            print(f"  {uri}  ({path})")
        print("resolution (public URI -> package member):")
        for f in batch.files:
            flag = " [entry]" if f.is_entry else ""
            print(f"  {f.public_uri}")
            print(f"      -> {f.member_path}  ({f.member_sha256[:16]}...){flag}")
        if batch.edges:
            print("dependency edges:")
            for e in batch.edges:
                cycle = " [CYCLE]" if e.is_cycle else ""
                print(f"  {e.kind}: {e.source_uri}")
                print(f"      --{e.raw_href!r}--> {e.target_uri}{cycle}")
    _emit(payload, args.json, human)
    return 0


def _cmd_verify(registry: TaxonomyRegistry, args) -> int:
    report = registry.verify()
    payload = {
        "ok": report.ok,
        "package_count": report.package_count,
        "members_verified": report.members_verified,
        "bytes_verified": report.bytes_verified,
        "batches_verified": report.batches_verified,
        "problems": list(report.problems),
    }

    def human():
        print(f"verified {report.package_count} package(s),"
              f" {report.members_verified} member(s),"
              f" {report.bytes_verified} byte(s),"
              f" {report.batches_verified} resolution batch(es)")
        for problem in report.problems:
            print(f"  PROBLEM: {problem}")
        print("registry OK" if report.ok else "registry FAILED verification")
    _emit(payload, args.json, human)
    return 0 if report.ok else 3


def _cmd_query(registry: TaxonomyRegistry, args) -> int:
    payload = registry.query_uri(args.digest, args.uri)

    def human():
        print(f"query: {payload['query']}")
        if payload["member"]:
            m = payload["member"]
            print(f"  registered member: {m['member_path']}"
                  f" (digest {m['blob_digest'][:16]}..., {m['byte_size']} bytes)")
        else:
            print("  registered member: (none match as a member path)")
        if payload["resolution_files"]:
            print("  resolution records:")
            for row in payload["resolution_files"]:
                print(f"    batch {row['batch_id']}: {row['public_uri']}"
                      f" -> {row['member_path']}")
        if payload["edges"]:
            print("  matching edges:")
            for row in payload["edges"]:
                print(f"    batch {row['batch_id']}: {row['kind']}"
                      f" {row['source_uri']} --{row['raw_href']!r}-->"
                      f" {row['target_uri']}")
    _emit(payload, args.json, human)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
