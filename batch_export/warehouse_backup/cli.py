"""Terminal commands for the warehouse backup tool.

    python3 -m warehouse_backup.cli create  BACKUP_DIR --db WAREHOUSE.sqlite [--parent N]
    python3 -m warehouse_backup.cli list    BACKUP_DIR
    python3 -m warehouse_backup.cli show    BACKUP_DIR --id N
    python3 -m warehouse_backup.cli check   BACKUP_DIR [--id N]
    python3 -m warehouse_backup.cli restore BACKUP_DIR --id N --target OUT.sqlite [--replace]
    python3 -m warehouse_backup.cli prune   BACKUP_DIR --keep N [--keep M ...] [--apply]

Exit statuses (designed for automation):

* 0  success;
* 2  invocation error (unknown backup id, missing source, existing target
    without --replace, unknown --keep id);
* 3  backup store corruption (invalid manifest, missing parent, cycle,
    missing/corrupt chunk, symlink substitution, digest or integrity-check
    failure) -- nothing is deleted and no target is touched;
* 4  output write failure (staging file, fsync, final rename).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .errors import (
    EXIT_OK,
    BackupError,
    InvocationError,
)
from .service import (
    apply_prune,
    check_backups,
    create_backup,
    load_chain,
    plan_prune,
    resolve_chunks,
    restore_backup,
)
from .store import BackupStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="warehouse_backup", description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="consistent backup of a live database")
    p.add_argument("backup_dir")
    p.add_argument("--db", required=True, help="live SQLite database to back up")
    p.add_argument("--parent", type=int,
                   help="existing backup id in the same directory to base this"
                        " incremental backup on (omit for a full root)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="list published backups")
    p.add_argument("backup_dir")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="manifest detail and per-layer chunk resolution")
    p.add_argument("backup_dir")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("check", help="verify manifests, chains and chunks")
    p.add_argument("backup_dir")
    p.add_argument("--id", type=int, help="check only this backup (default: all)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("restore", help="assemble one backup at an offline target")
    p.add_argument("backup_dir")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--target", required=True,
                   help="offline output path (must be outside the backup dir)")
    p.add_argument("--replace", action="store_true",
                   help="allow replacing an existing target; any failure keeps"
                        " the original bytes")
    p.add_argument("--pause-gate", metavar="PATH",
                   help="drill/testing hook: after the dependency chain is"
                        " resolved, signal PATH.acquired and wait for PATH.go")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("prune", help="preview (default) or apply retention")
    p.add_argument("backup_dir")
    p.add_argument("--keep", type=int, action="append", default=[],
                   metavar="ID", help="backup id to keep; repeatable")
    p.add_argument("--apply", action="store_true",
                   help="actually delete (default is preview only)")
    p.add_argument("--json", action="store_true")
    return parser


def _emit(payload, as_json: bool, human) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        human()


def _cmd_create(store: BackupStore, args) -> int:
    report = create_backup(store, args.db, parent=args.parent)
    payload = {
        "backup_id": report.backup_id,
        "parent": report.parent,
        "image_size": report.image_size,
        "image_sha256": report.image_sha256,
        "chunk_count": report.chunk_count,
        "layer_chunks": report.layer_chunks,
        "new_chunk_files": report.new_chunk_files,
        "reused_chunk_files": report.reused_chunk_files,
    }

    def human():
        kind = "root" if report.parent is None else f"child of {report.parent:06d}"
        print(f"backup {report.backup_id:06d} created ({kind})")
        print(f"  image: {report.image_size} bytes,"
              f" sha256 {report.image_sha256}")
        print(f"  chunks: {report.chunk_count} total,"
              f" {report.layer_chunks} listed in this layer"
              f" ({report.new_chunk_files} new files,"
              f" {report.reused_chunk_files} shared reused)")
    _emit(payload, args.json, human)
    return EXIT_OK


def _cmd_list(store: BackupStore, args) -> int:
    with store.locked(exclusive=False):
        store.check_layout()
        manifests = store.load_all_manifests()
    payload = [
        {
            "id": m.id,
            "parent": m.parent,
            "created_at": m.created_at,
            "image_size": m.image_size,
            "image_sha256": m.image_sha256,
            "chunk_count": m.chunk_count,
            "layer_chunks": len(m.chunks),
        }
        for m in manifests
    ]

    def human():
        if not manifests:
            print("no backups")
            return
        for m in manifests:
            parent = "root" if m.parent is None else f"parent {m.parent:06d}"
            print(f"{m.id:06d}  {parent:<16} image {m.image_size:>12} bytes"
                  f"  chunks {len(m.chunks):>4}/{m.chunk_count:<4}"
                  f"  {m.created_at}")
    _emit(payload, args.json, human)
    return EXIT_OK


def _cmd_show(store: BackupStore, args) -> int:
    with store.locked(exclusive=False):
        store.check_layout()
        if not store.manifest_exists(args.id):
            raise InvocationError(f"backup {args.id:06d} does not exist in"
                                  f" {store.root}")
        chain = load_chain(store, args.id)
        resolution = resolve_chunks(chain)
    head = chain[0]
    # Group consecutive chunk indices by the layer that provides them.
    spans = []
    for index in range(head.chunk_count):
        layer_id, entry = resolution[index]
        if spans and spans[-1]["layer"] == layer_id:
            spans[-1]["last"] = index
        else:
            spans.append({"layer": layer_id, "first": index, "last": index})
    payload = {
        "manifest": head.to_json(),
        "chain": [m.id for m in chain],
        "chunk_sources": spans,
    }

    def human():
        print(f"backup {head.id:06d}  created {head.created_at}")
        print(f"  parent: {head.parent if head.parent is not None else 'none (root)'}")
        print(f"  image: {head.image_size} bytes  sha256 {head.image_sha256}")
        print(f"  chunks: {head.chunk_count} x 65536 (last may be short),"
              f" {len(head.chunks)} stored in this layer")
        print(f"  chain to root: {' -> '.join(f'{m.id:06d}' for m in chain)}")
        print("  chunk source layers (nearest layer on the chain wins):")
        for span in spans:
            print(f"    chunks {span['first']:>6}..{span['last']:<6}"
                  f" from layer {span['layer']:06d}")
    _emit(payload, args.json, human)
    return EXIT_OK


def _cmd_check(store: BackupStore, args) -> int:
    report = check_backups(store, backup_id=args.id)
    payload = {
        "ok": report.ok,
        "backups": [
            {"id": c.backup_id, "ok": c.ok, "layers": c.layers,
             "chunks_verified": c.chunks_verified,
             "bytes_verified": c.bytes_verified}
            for c in report.checks
        ],
        "unreferenced_chunks": report.unreferenced_chunks,
    }

    def human():
        for c in report.checks:
            print(f"{c.backup_id:06d}  ok  ({c.layers} layer(s),"
                  f" {c.chunks_verified} chunks,"
                  f" {c.bytes_verified} bytes verified)")
        if report.unreferenced_chunks:
            print(f"note: {report.unreferenced_chunks} chunk file(s) are not"
                  f" referenced by any manifest (prune will reclaim them)")
    _emit(payload, args.json, human)
    return EXIT_OK


def _cmd_restore(store: BackupStore, args) -> int:
    report = restore_backup(store, args.id, args.target, replace=args.replace,
                            pause_gate=args.pause_gate)
    payload = {
        "backup_id": report.backup_id,
        "target": report.target,
        "image_size": report.image_size,
        "image_sha256": report.image_sha256,
        "layers": list(report.layers),
        "integrity_check": report.integrity_check,
    }

    def human():
        chain = " -> ".join(f"{layer:06d}" for layer in report.layers)
        print(f"restored backup {report.backup_id:06d} to {report.target}")
        print(f"  chain: {chain}")
        print(f"  image: {report.image_size} bytes,"
              f" sha256 {report.image_sha256}")
        print(f"  SQLite integrity_check: {report.integrity_check}")
    _emit(payload, args.json, human)
    return EXIT_OK


def _cmd_prune(store: BackupStore, args) -> int:
    if not args.keep:
        raise InvocationError(
            "prune needs at least one --keep id (refusing to guess an empty"
            " keep set; remove the directory yourself to delete everything)")
    keep = tuple(args.keep)
    if args.apply:
        plan = apply_prune(store, keep)
    else:
        plan = plan_prune(store, keep)
    payload = {
        "applied": bool(args.apply),
        "keep": list(plan.keep),
        "needed_dependencies": list(plan.needed_dependencies),
        "removable_manifests": list(plan.removable_manifests),
        "removable_chunks": len(plan.removable_chunks),
        "unreferenced_chunks": len(plan.unreferenced_chunks),
        "staging_files": list(plan.staging_files),
        "bytes_freed": plan.bytes_freed,
    }

    def human():
        verb = "prune applied" if args.apply else "prune preview (nothing deleted)"
        print(f"{verb}: keeping {', '.join(f'{k:06d}' for k in plan.keep) or '(none)'}")
        if plan.needed_dependencies:
            print(f"  still needed as dependencies:"
                  f" {', '.join(f'{d:06d}' for d in plan.needed_dependencies)}")
        if plan.removable_manifests:
            print(f"  removable manifests:"
                  f" {', '.join(f'{m:06d}' for m in plan.removable_manifests)}")
        else:
            print("  no manifests can be removed")
        print(f"  removable chunks: {len(plan.removable_chunks)}"
              f" ({plan.bytes_freed} bytes)")
        if plan.unreferenced_chunks:
            print(f"  of which unreferenced by any manifest:"
                  f" {len(plan.unreferenced_chunks)}")
        if plan.staging_files:
            print(f"  leftover staging files: {len(plan.staging_files)}")
    _emit(payload, args.json, human)
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = BackupStore(args.backup_dir)
    try:
        if args.command == "create":
            return _cmd_create(store, args)
        if args.command == "list":
            return _cmd_list(store, args)
        if args.command == "show":
            return _cmd_show(store, args)
        if args.command == "check":
            return _cmd_check(store, args)
        if args.command == "restore":
            return _cmd_restore(store, args)
        if args.command == "prune":
            return _cmd_prune(store, args)
    except BackupError as exc:
        print(f"{exc.category} error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 1  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
