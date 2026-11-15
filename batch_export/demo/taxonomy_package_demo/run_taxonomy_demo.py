#!/usr/bin/env python3
"""End-to-end drill for the offline taxonomy package registry.

Stages (each prints ok/FAIL and contributes explicit assertions):

  1. build the demo package (overlapping prefixes, import, xml:base include,
     two entries sharing a dependency, linkbaseRef, dependency cycle, junk);
  2. register it; re-register the identical ZIP and prove no new copy;
  3. resolve both entries and compare every file/edge against an independent
     checker that reads the ZIP with stdlib zipfile + ElementTree only;
  4. demonstrate longest-prefix matching and xml:base include resolution;
  5. negative fixtures, each required to fail non-zero and leave the
     registry byte-identical: path escape, same-prefix conflict, missing
     dependency, identifier/version content conflict, external entity,
     corrupted member digest;
  6. relocate the registry to an offline location with no ZIP and no
     network, re-verify and re-resolve, then tamper a dependency and prove
     the offline check names the changed member;
  7. scale: many entries sharing dependencies plus many unrelated ZIP
     members -- only closure XML is parsed, output stays stable;
  8. repeated runs never change registry digests or add copies.

Run from anywhere:  python3 -I demo/taxonomy_package_demo/run_taxonomy_demo.py
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
# Run under "python3 -I": the interpreter then ignores PYTHONPATH and user
# site-packages, so add back only the two trusted code locations explicitly.
# lxml is installed in the user site in this environment; nothing from the
# data/temp directories is ever placed on sys.path.
import site  # noqa: E402

for candidate in (
    PROJECT,
    HERE,
    site.getusersitepackages(),
):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import fixture_builder as fb  # noqa: E402
import independent_check as ic  # noqa: E402
from taxonomy_package import (  # noqa: E402
    CatalogConflictError,
    PackageFormatError,
    RegistryCorruptionError,
    ResolutionError,
    TaxonomyRegistry,
    VersionConflictError,
)
from taxonomy_package.cli import main as cli_main  # noqa: E402

CAT_NS = "urn:oasis:names:tc:entity:xmlns:xml:catalog"
TP_NS = "http://xbrl.org/2016/taxonomy-package"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
LINK_NS = "http://www.xbrl.org/2003/linkbase"
XLINK_NS = "http://www.w3.org/1999/xlink"


class Checker:
    def __init__(self):
        self.checks = 0
        self.failures: list[str] = []

    def check(self, label, actual, expected):
        self.checks += 1
        if actual != expected:
            self.failures.append(f"{label}: expected {expected!r}, got {actual!r}")
            print(f"  FAIL {label}")
        else:
            print(f"  ok {label}")

    def check_true(self, label, value):
        self.check(label, bool(value), True)

    def check_raises(self, label, exc_type, fn):
        self.checks += 1
        try:
            fn()
        except exc_type as exc:
            print(f"  ok {label} ({type(exc).__name__})")
            return exc
        except BaseException as exc:  # noqa: BLE001
            self.failures.append(
                f"{label}: expected {exc_type.__name__}, got"
                f" {type(exc).__name__}: {exc}")
            print(f"  FAIL {label}: wrong exception {type(exc).__name__}")
        else:
            self.failures.append(f"{label}: no exception raised")
            print(f"  FAIL {label}: no exception")
        return None

    def finish(self) -> int:
        if self.failures:
            print(f"\n{len(self.failures)} of {self.checks} checks failed")
            return 1
        print(f"\ndrill complete: all {self.checks} checks held")
        return 0


def digest_tree(root: str) -> str:
    hasher = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for fn in sorted(files):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            hasher.update(rel.encode())
            with open(full, "rb") as handle:
                hasher.update(handle.read())
    return hasher.hexdigest()


def make_zip(path: str, members: dict, *, symlinks=()) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            info = zipfile.ZipInfo(name)
            if name in symlinks:
                import stat as stat_mod
                info.external_attr = (stat_mod.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)


def descriptor(entries, identifier="com.example.drill", version="1.0.0"):
    eps = "".join(
        f"<tp:entryPoint><tp:entryPointDocument href=\"{h}\"/>"
        "</tp:entryPoint>" for h in entries)
    return f"""<?xml version="1.0"?>
<tp:taxonomyPackage xmlns:tp="{TP_NS}">
  <tp:identifier>{identifier}</tp:identifier>
  <tp:name>Drill</tp:name>
  <tp:version>{version}</tp:version>
  <tp:entryPoints>{eps}</tp:entryPoints>
</tp:taxonomyPackage>"""


def catalog_xml(rules):
    body = "".join(
        f'<rewriteURI uriStartString="{s}" rewritePrefix="{p}"/>'
        for s, p in rules)
    return f'<?xml version="1.0"?><catalog xmlns="{CAT_NS}">{body}</catalog>'


def schema(imports=(), includes=(), *, xml_base=None, linkbase=False):
    attrs = ' targetNamespace="urn:drill"'
    if xml_base:
        attrs += f' xml:base="{xml_base}"'
    parts = []
    for href in imports:
        parts.append(
            f'<import namespace="u" schemaLocation="{href}"/>')
    for href in includes:
        parts.append(f'<include schemaLocation="{href}"/>')
    if linkbase:
        parts.append(
            '<annotation><appinfo>'
            f'<link:linkbaseRef xmlns:link="{LINK_NS}"'
            f' xmlns:xlink="{XLINK_NS}" xlink:type="simple"'
            ' xlink:href="lb.xml"/></appinfo></annotation>')
    return ('<?xml version="1.0"?>'
            f'<schema xmlns="{XSD_NS}"{attrs}>' + "".join(parts) +
            '</schema>')


def main() -> int:
    check = Checker()
    work = tempfile.mkdtemp(prefix="tp_drill_")
    try:
        registry_path = os.path.join(work, "registry")
        registry = TaxonomyRegistry(registry_path, create=True)

        # -- stage 1/2: build + register + idempotent reuse ------------------
        print("stage 1-2: build, register, idempotent reuse")
        demo_zip = os.path.join(work, "demo.zip")
        fb.build_zip(demo_zip, junk_count=200)
        result = registry.register_zip(demo_zip)
        digest = result.package_sha256
        check.check("member count includes junk",
                    result.member_count, len(fb.MEMBERS) + 220)
        before_copy = digest_tree(registry_path)
        again = registry.register_zip(demo_zip)
        check.check("re-register identical ZIP is reused", again.reused, True)
        check.check("re-register adds no bytes",
                    digest_tree(registry_path), before_copy)
        check.check("one package directory",
                    registry.store.list_package_dirs(), [digest])

        # -- stage 3: independent closure comparison --------------------------
        print("stage 3: independent closure comparison")
        batch = registry.resolve_entries(digest)
        expected = ic.expected_closure(demo_zip)
        check.check("file set matches independent reader",
                    {f.public_uri: f.member_path for f in batch.files},
                    expected["files"])
        actual_edges = tuple(sorted(
            (e.source_uri, e.target_uri, e.kind, e.is_cycle)
            for e in batch.edges))
        check.check("edges match independent reader",
                    actual_edges, expected["edges"])
        check.check("exactly one cycle edge",
                    batch.cycle_edge_count, 1)
        check.check("each file recorded once",
                    len(batch.files),
                    len({f.public_uri for f in batch.files}))

        # -- stage 4: longest prefix + xml:base -------------------------------
        print("stage 4: longest prefix and xml:base behaviour")
        by_member = {f.member_path: f for f in batch.files}
        check.check_true("long prefix reaches schemas/ext",
                         by_member["schemas/ext/ext-types.xsd"].public_uri
                         .startswith("xbrl-pkg://package/schemas/ext/"))
        check.check_true("short prefix reaches schemas/base",
                         by_member["schemas/base/types.xsd"].public_uri
                         .startswith("xbrl-pkg://package/schemas/base/"))
        check.check_true("xml:base include reached parts/types-base.xsd",
                         "schemas/base/parts/types-base.xsd" in by_member)
        check.check_true("linkbases/labels.xml is in the closure",
                         "linkbases/labels.xml" in by_member)
        # Shared dependency: resolving each entry alone still finds types.xsd.
        ep1 = "xbrl-pkg://package/schemas/ep1.xsd"
        ep2 = "xbrl-pkg://package/schemas/ep2.xsd"
        b1 = registry.resolve_entries(digest, (ep1,))
        b2 = registry.resolve_entries(digest, (ep2,))
        shared = "xbrl-pkg://package/schemas/base/types.xsd"
        check.check_true("ep1 closure contains shared types.xsd",
                         any(f.public_uri == shared for f in b1.files))
        check.check_true("ep2 closure contains shared types.xsd",
                         any(f.public_uri == shared for f in b2.files))

        # -- stage 5: negative fixtures ---------------------------------------
        print("stage 5: negative fixtures")
        # Snapshot the demo package directory specifically: the negative
        # fixtures that are themselves valid packages get registered (the
        # failure happens later at resolution), but they must never alter the
        # already-published demo package.
        def demo_digest():
            return digest_tree(registry.store.package_dir(digest))

        reg_before = demo_digest()

        # 5a path escape
        esc_zip = os.path.join(work, "escape.zip")
        esc = {f"p/META-INF/taxonomyPackage.xml":
                   descriptor(("schemas/entry.xsd",)),
               "p/META-INF/catalog.xml": catalog_xml(
                   [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
               "p/schemas/entry.xsd": schema(),
               "p/schemas/../evil.xsd": b"x"}
        make_zip(esc_zip, esc)
        check.check_raises("path traversal member refused",
                           PackageFormatError,
                           lambda: registry.register_zip(esc_zip))
        check.check("demo package unchanged after path escape",
                    demo_digest(), reg_before)
        check.check_true("no escape file written",
                         not os.path.exists(os.path.join(work, "evil.xsd")))

        # 5b same prefix, different targets
        conflict_zip = os.path.join(work, "conflict.zip")
        make_zip(conflict_zip, {
            "p/META-INF/taxonomyPackage.xml":
                descriptor(("schemas/entry.xsd",), identifier="conflict-pkg"),
            "p/META-INF/catalog.xml": catalog_xml([
                ("http://x.example/ns", "xbrl-pkg://package/a/"),
                ("http://x.example/ns", "xbrl-pkg://package/b/"),
            ]),
            "p/schemas/entry.xsd": schema(),
        })
        check.check_raises("same prefix different target refused",
                           CatalogConflictError,
                           lambda: registry.register_zip(conflict_zip))

        # 5c missing dependency
        miss_zip = os.path.join(work, "missing.zip")
        make_zip(miss_zip, {
            "p/META-INF/taxonomyPackage.xml":
                descriptor(("schemas/entry.xsd",), identifier="missing-pkg"),
            "p/META-INF/catalog.xml": catalog_xml(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": schema(
                imports=("http://x.example/ns/ghost.xsd",)),
        })
        miss_result = registry.register_zip(miss_zip)
        check.check_raises("missing dependency refused at resolution",
                           ResolutionError,
                           lambda: registry.resolve_entries(
                               miss_result.package_sha256))
        check.check("no batch persisted after failed resolution",
                    registry.show_package(
                        miss_result.package_sha256)["batches"], [])

        # 5d identifier/version content conflict
        conflict2_zip = os.path.join(work, "idver.zip")
        make_zip(conflict2_zip, {
            "p/META-INF/taxonomyPackage.xml":
                descriptor(("schemas/entry.xsd",),
                           identifier="com.example.demo-taxonomy",
                           version="1.0.0"),
            "p/META-INF/catalog.xml": catalog_xml(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": schema(includes=("extra.xsd",)),
        })
        check.check_raises("same id/version different bytes refused",
                           VersionConflictError,
                           lambda: registry.register_zip(conflict2_zip))

        # 5e external entity
        xxe_zip = os.path.join(work, "xxe.zip")
        make_zip(xxe_zip, {
            "p/META-INF/taxonomyPackage.xml":
                descriptor(("schemas/entry.xsd",), identifier="xxe-pkg"),
            "p/META-INF/catalog.xml": catalog_xml(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd":
                '<?xml version="1.0"?><!DOCTYPE schema ['
                '<!ENTITY x SYSTEM "file:///etc/hostname">]>'
                f'<schema xmlns="{XSD_NS}">&x;</schema>',
        })
        check.check_raises("external entity refused",
                           PackageFormatError,
                           lambda: registry.register_zip(xxe_zip))

        check.check("demo package unchanged after all negatives",
                    demo_digest(), reg_before)

        # 5f corrupted member digest (tamper post-registration)
        print("stage 5f: corrupted member digest")
        detail = registry.show_package(digest)
        target = next(m for m in detail["members"]
                      if m["member_path"] == "schemas/base/types.xsd")
        blob = registry.store.blob_path(digest, target["blob_digest"])
        with open(blob, "wb") as handle:
            handle.write(b"<!-- tampered -->")
        report = registry.verify()
        check.check_true("tamper makes verify fail", not report.ok)
        check.check_true("verify names the changed member",
                         any("schemas/base/types.xsd" in p
                             for p in report.problems))
        # Restore the bytes so later stages see a clean registry.
        with zipfile.ZipFile(demo_zip) as archive:
            top = archive.namelist()[0].split("/", 1)[0]
            original = archive.read(f"{top}/schemas/base/types.xsd")
        with open(blob, "wb") as handle:
            handle.write(original)
        check.check_true("restored registry verifies again",
                         registry.verify().ok)

        # -- stage 6: offline relocation --------------------------------------
        print("stage 6: offline relocation without ZIP or network")
        offline = os.path.join(work, "offline-registry")
        shutil.copytree(registry_path, offline)
        os.remove(demo_zip)  # original ZIP is gone in the offline world
        offline_reg = TaxonomyRegistry(offline)
        report = offline_reg.verify()
        check.check_true("offline verify passes with no ZIP", report.ok)
        off_batch = offline_reg.resolve_entries(digest)
        check.check("offline resolution yields same file count",
                    off_batch.file_count, batch.file_count)
        check.check("offline resolution yields same edges",
                    len(off_batch.edges), len(batch.edges))
        # Tamper a dependency in the offline copy.
        off_detail = offline_reg.show_package(digest)
        dep = next(m for m in off_detail["members"]
                   if m["member_path"] == "schemas/cycle/cyclic-a.xsd")
        dep_blob = offline_reg.store.blob_path(digest, dep["blob_digest"])
        with open(dep_blob, "wb") as handle:
            handle.write(b"changed offline")
        report = offline_reg.verify()
        check.check_true("offline tamper detected", not report.ok)
        check.check_true("offline check names cyclic-a.xsd",
                         any("cyclic-a.xsd" in p for p in report.problems))

        # -- stage 7: scale, only closure XML parsed --------------------------
        print("stage 7: scale and closure-only parsing")
        scale_zip = os.path.join(work, "scale.zip")
        _build_scale_package(scale_zip, entries=12, junk=400)
        scale_reg_path = os.path.join(work, "scale-registry")
        scale_reg = TaxonomyRegistry(scale_reg_path, create=True)
        scale_result = scale_reg.register_zip(scale_zip)
        scale_batch = scale_reg.resolve_entries(scale_result.package_sha256)
        # 12 entries + 1 shared types schema = 13 XML files, despite 400
        # junk members (300 .txt + 100 stray .xsd).
        check.check("scale closure parses only dependency XML",
                    scale_batch.file_count, 13)
        check.check_true("no junk member entered the closure",
                         all(not f.member_path.startswith("junk/")
                             for f in scale_batch.files))
        # Stable output across two independent registrations.
        scale_reg2_path = os.path.join(work, "scale-registry2")
        scale_reg2 = TaxonomyRegistry(scale_reg2_path, create=True)
        r2 = scale_reg2.register_zip(scale_zip)
        batch2 = scale_reg2.resolve_entries(r2.package_sha256)
        check.check("scale output is stable",
                    [(f.public_uri, f.member_path) for f in scale_batch.files],
                    [(f.public_uri, f.member_path) for f in batch2.files])

        # -- stage 8: repeated runs do not change digests ---------------------
        print("stage 8: repeat stability")
        stable_before = digest_tree(offline)
        # Re-resolve everything repeatedly; closure batches are keyed by entry
        # sets so repeats return existing batch ids and never write again.
        ids = [offline_reg.resolve_entries(digest).batch_id for _ in range(3)]
        check.check("repeated resolve returns same batch", len(set(ids)), 1)
        # Re-register into the scale registry: no copy, no digest change.
        scale_digest_before = digest_tree(scale_reg_path)
        repeated = scale_reg.register_zip(scale_zip)
        check.check_true("scale re-register is reuse", repeated.reused)
        check.check("scale registry digest stable",
                    digest_tree(scale_reg_path), scale_digest_before)
        # Read-only operations never modify the offline registry tree.
        offline_reg.verify()
        offline_reg.list_packages()
        check.check("read-only operations leave the registry tree unchanged",
                    digest_tree(offline), stable_before)

        # -- CLI exit-code spot checks ----------------------------------------
        print("stage 9: CLI exit codes")
        rc = cli_main(["verify", offline])
        check.check("CLI verify returns 3 on tampered offline copy", rc, 3)
        rc = cli_main(["list", scale_reg_path, "--json"])
        check.check("CLI list returns 0", rc, 0)
        rc = cli_main(["show", scale_reg_path, "--digest", "z" * 64])
        check.check("CLI bad digest returns 2", rc, 2)

    finally:
        shutil.rmtree(work, True)
    return check.finish()


def _build_scale_package(path: str, *, entries: int, junk: int) -> None:
    """Many entries sharing one imported type schema, plus many junk members."""
    public = "http://scale.example/ns"
    members = {
        "scale/META-INF/taxonomyPackage.xml":
            descriptor(tuple(f"schemas/entry-{i:02d}.xsd" for i in range(entries)),
                       identifier="scale-pkg"),
        "scale/META-INF/catalog.xml": catalog_xml(
            [(public, "xbrl-pkg://package/schemas/")]),
        "scale/schemas/types.xsd": schema(),
    }
    for i in range(entries):
        members[f"scale/schemas/entry-{i:02d}.xsd"] = schema(
            imports=(f"{public}/types.xsd",))
    for i in range(junk):
        suffix = "txt" if i % 4 else "xsd"
        members[f"scale/junk/unrelated-{i:04d}.{suffix}"] = (
            '<?xml version="1.0"?><schema xmlns="'
            f'{XSD_NS}"/>') if suffix == "xsd" else f"note {i}"
    make_zip(path, members)


if __name__ == "__main__":
    raise SystemExit(main())
