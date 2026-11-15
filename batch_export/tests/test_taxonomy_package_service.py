"""Service-level tests: registration, resolution, verification, negatives.

Every failure case must (a) raise/exit non-zero and (b) leave the registry
byte-identical to its state before the rejected operation.
"""
import hashlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "demo", "taxonomy_package_demo"))

import fixture_builder as fb  # noqa: E402
from taxonomy_package import (  # noqa: E402
    PackageFormatError,
    RegistryCorruptionError,
    ResolutionError,
    TaxonomyRegistry,
    VersionConflictError,
)
from taxonomy_package.errors import (  # noqa: E402
    CatalogConflictError,
    InvocationError,
)

CAT_NS = "urn:oasis:names:tc:entity:xmlns:xml:catalog"
TP_NS = "http://xbrl.org/2016/taxonomy-package"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
LINK_NS = "http://www.xbrl.org/2003/linkbase"
XLINK_NS = "http://www.w3.org/1999/xlink"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zip_to_bytes(members: dict, *, symlinks=()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            if name in symlinks:
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            else:
                info = zipfile.ZipInfo(name)
            archive.writestr(info, data)
    return buf.getvalue()


def simple_schema(imports=(), includes=(), *, xml_base=None,
                  with_linkbase=False) -> str:
    attrs = f' targetNamespace="urn:x"'
    if xml_base:
        attrs += f' xml:base="{xml_base}"'
    body = []
    for href in imports:
        body.append(f'  <import namespace="urn:imp" schemaLocation="{href}"/>')
    for href in includes:
        body.append(f'  <include schemaLocation="{href}"/>')
    if with_linkbase:
        body.append(
            '  <annotation><appinfo>'
            f'<link:linkbaseRef xmlns:link="{LINK_NS}"'
            f' xmlns:xlink="{XLINK_NS}" xlink:type="simple"'
            ' xlink:href="lb.xml"/></appinfo></annotation>')
    return ('<?xml version="1.0"?>'
            f'<schema xmlns="{XSD_NS}"{attrs}>' + "".join(body) +
            '</schema>')


def descriptor(entries=("schemas/entry.xsd",), identifier="id-x",
               version="1.0") -> str:
    ep = "".join(
        f"<tp:entryPoint><tp:entryPointDocument href=\"{h}\"/></tp:entryPoint>"
        for h in entries)
    return f"""<?xml version="1.0"?>
<tp:taxonomyPackage xmlns:tp="{TP_NS}">
  <tp:identifier>{identifier}</tp:identifier>
  <tp:name>Name</tp:name>
  <tp:version>{version}</tp:version>
  <tp:entryPoints>{ep}</tp:entryPoints>
</tp:taxonomyPackage>"""


def catalog(rules) -> str:
    body = "".join(
        f'<rewriteURI uriStartString="{s}" rewritePrefix="{p}"/>'
        for s, p in rules)
    return f'<?xml version="1.0"?><catalog xmlns="{CAT_NS}">{body}</catalog>'


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tp_svc_")
        self.registry_path = os.path.join(self.tmp, "registry")
        self.registry = TaxonomyRegistry(self.registry_path, create=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def write_zip(self, name: str, members: dict, **kw) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(zip_to_bytes(members, **kw))
        return path

    def registry_digest(self) -> str:
        """Content digest of every file in the registry (order-independent)."""
        hasher = hashlib.sha256()
        for root, _dirs, files in os.walk(self.registry_path):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                if os.path.islink(full):
                    hasher.update(b"SYMLINK:" + fn.encode())
                    continue
                with open(full, "rb") as handle:
                    hasher.update(fn.encode())
                    hasher.update(handle.read())
        return hasher.hexdigest()

    def assert_registry_unchanged(self, before: str) -> None:
        self.assertEqual(self.registry_digest(), before,
                         "a rejected operation modified the registry")


class RegistrationTest(ServiceTestBase):
    def test_register_demo_and_idempotent_reuse(self):
        path = self.write_zip("demo.zip",
                              {f"{fb.TOP}/{k}": v
                               for k, v in fb.MEMBERS.items()})
        result = self.registry.register_zip(path)
        self.assertFalse(result.reused)
        again = self.registry.register_zip(path)
        self.assertTrue(again.reused)
        self.assertEqual(again.package_sha256, result.package_sha256)
        # No duplicate package directory.
        self.assertEqual(
            self.registry.store.list_package_dirs(), [result.package_sha256])

    def test_version_conflict_same_identifier_version_different_bytes(self):
        members1 = {
            "p/META-INF/taxonomyPackage.xml": descriptor(),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": simple_schema(),
        }
        path1 = self.write_zip("v1.zip", members1)
        r1 = self.registry.register_zip(path1)
        before = self.registry_digest()
        # Same identifier+version, different content.
        members2 = dict(members1)
        members2["p/schemas/entry.xsd"] = simple_schema(
            includes=("other.xsd",))
        path2 = self.write_zip("v1b.zip", members2)
        with self.assertRaises(VersionConflictError):
            self.registry.register_zip(path2)
        self.assert_registry_unchanged(before)
        self.assertEqual(self.registry.store.list_package_dirs(),
                         [r1.package_sha256])

    def test_different_version_same_identifier_allowed(self):
        members1 = {
            "p/META-INF/taxonomyPackage.xml": descriptor(version="1.0"),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": simple_schema(),
        }
        r1 = self.registry.register_zip(self.write_zip("v1.zip", members1))
        members2 = {
            "p/META-INF/taxonomyPackage.xml": descriptor(version="2.0"),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": simple_schema(),
        }
        r2 = self.registry.register_zip(self.write_zip("v2.zip", members2))
        self.assertNotEqual(r1.package_sha256, r2.package_sha256)
        self.assertEqual(len(self.registry.store.list_package_dirs()), 2)

    def test_publish_failure_leaves_no_partial_directory(self):
        from taxonomy_package import store as store_mod
        from taxonomy_package.errors import PublishError
        members = {
            "p/META-INF/taxonomyPackage.xml": descriptor(
                identifier="pub-fail"),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": simple_schema(),
        }
        path = self.write_zip("good.zip", members)
        original = store_mod.TaxonomyStore.write_blob

        def fail(self, staging, digest, data):
            raise PublishError("injected disk failure")

        store_mod.TaxonomyStore.write_blob = fail
        try:
            with self.assertRaises(PublishError):
                self.registry.register_zip(path)
        finally:
            store_mod.TaxonomyStore.write_blob = original
        # No published package, no staging leftover.
        self.assertEqual(self.registry.store.list_package_dirs(), [])
        leftovers = [
            e.name for e in self.registry.store.packages_dir.iterdir()
            if ".staging-" in e.name]
        self.assertEqual(leftovers, [])
        # The registry still accepts the package after recovery.
        result = self.registry.register_zip(path)
        self.assertEqual(
            self.registry.store.list_package_dirs(),
            [result.package_sha256])


class NegativeCaseTest(ServiceTestBase):
    def _base_members(self):
        return {
            "p/META-INF/taxonomyPackage.xml": descriptor(),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": simple_schema(),
        }

    def test_path_escape_in_zip_refused_and_registry_untouched(self):
        path = self.write_zip("good.zip", self._base_members())
        self.registry.register_zip(path)
        before = self.registry_digest()
        evil = self._base_members()
        evil["p/schemas/../../escape.xsd"] = b"x"
        bad = self.write_zip("evil.zip", evil)
        with self.assertRaises(PackageFormatError):
            self.registry.register_zip(bad)
        self.assert_registry_unchanged(before)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "escape.xsd")))

    def test_symlink_member_refused(self):
        before = self.registry_digest()
        evil = self._base_members()
        evil["p/link.xsd"] = "entry.xsd"
        bad = self.write_zip("sym.zip", evil, symlinks=("p/link.xsd",))
        with self.assertRaises(PackageFormatError):
            self.registry.register_zip(bad)
        self.assert_registry_unchanged(before)

    def test_same_prefix_different_target_refused(self):
        before = self.registry_digest()
        members = self._base_members()
        members["p/META-INF/catalog.xml"] = catalog([
            ("http://x.example/ns", "xbrl-pkg://package/a/"),
            ("http://x.example/ns", "xbrl-pkg://package/b/"),
        ])
        with self.assertRaises(CatalogConflictError):
            self.registry.register_zip(
                self.write_zip("conflict.zip", members))
        self.assert_registry_unchanged(before)

    def test_missing_dependency_refused_at_resolution(self):
        members = {
            "p/META-INF/taxonomyPackage.xml": descriptor(),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": simple_schema(
                imports=("http://x.example/ns/missing.xsd",)),
        }
        result = self.registry.register_zip(
            self.write_zip("miss.zip", members))
        before = self.registry_digest()
        with self.assertRaises(ResolutionError) as ctx:
            self.registry.resolve_entries(result.package_sha256)
        self.assertIn("entry.xsd", str(ctx.exception))
        self.assert_registry_unchanged(before)
        # No batch got persisted.
        self.assertEqual(self.registry.show_package(
            result.package_sha256)["batches"], [])

    def test_external_entity_refused(self):
        before = self.registry_digest()
        members = self._base_members()
        members["p/schemas/entry.xsd"] = (
            '<?xml version="1.0"?><!DOCTYPE schema ['
            '<!ENTITY x SYSTEM "file:///etc/hostname">]>'
            f'<schema xmlns="{XSD_NS}">&x;</schema>')
        with self.assertRaises(PackageFormatError):
            self.registry.register_zip(
                self.write_zip("xxe.zip", members))
        self.assert_registry_unchanged(before)

    def test_doctype_in_dependency_refused_at_resolution(self):
        # Registration screens XML eagerly; prove the resolver itself also
        # refuses a DTD if such bytes ever reach it (defence in depth).
        from taxonomy_package.models import CatalogRule, PackageMember
        from taxonomy_package.resolver import SchemaResolver
        members = (
            PackageMember("entry.xsd", sha256_bytes(b"e"), 1, True),
            PackageMember("dep.xsd", sha256_bytes(b"d"), 1, True),
        )
        rules = (CatalogRule(
            uri_start_string="http://x.example/ns",
            rewrite_prefix="xbrl-pkg://package/",
            rewrite_prefix_raw="xbrl-pkg://package/", line=1),)
        payloads = {
            "entry.xsd":
                f'<?xml version="1.0"?><schema xmlns="{XSD_NS}">'
                '<import namespace="u"'
                ' schemaLocation="http://x.example/ns/dep.xsd"/></schema>'
                .encode(),
            "dep.xsd": b'<?xml version="1.0"?><!DOCTYPE r><r/>',
        }
        resolver = SchemaResolver(
            package_sha256="d" * 64, members=members,
            catalog_rules=rules, payloads=payloads)
        with self.assertRaises(ResolutionError) as ctx:
            resolver.resolve(("xbrl-pkg://package/entry.xsd",))
        self.assertIn("DOCTYPE", str(ctx.exception))

    def test_unknown_catalog_rule_refused(self):
        before = self.registry_digest()
        members = self._base_members()
        members["p/META-INF/catalog.xml"] = (
            f'<?xml version="1.0"?><catalog xmlns="{CAT_NS}">'
            '<uri name="http://x.example/u" uri="xbrl-pkg://package/u.xsd"/>'
            '</catalog>')
        with self.assertRaises(PackageFormatError):
            self.registry.register_zip(
                self.write_zip("unknown.zip", members))
        self.assert_registry_unchanged(before)

    def test_import_without_schema_location_refused(self):
        members = {
            "p/META-INF/taxonomyPackage.xml": descriptor(),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd":
                f'<?xml version="1.0"?><schema xmlns="{XSD_NS}">'
                '<import namespace="urn:imp"/></schema>',
        }
        result = self.registry.register_zip(
            self.write_zip("noloc.zip", members))
        with self.assertRaises(ResolutionError) as ctx:
            self.registry.resolve_entries(result.package_sha256)
        self.assertIn("schemaLocation", str(ctx.exception))

    def test_public_uri_without_rule_refused_no_network(self):
        members = {
            "p/META-INF/taxonomyPackage.xml": descriptor(),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": simple_schema(
                imports=("http://other.example/unmapped.xsd",)),
        }
        result = self.registry.register_zip(
            self.write_zip("net.zip", members))
        with self.assertRaises(ResolutionError) as ctx:
            self.registry.resolve_entries(result.package_sha256)
        self.assertIn("no matching catalog", str(ctx.exception))

    def test_longest_prefix_resolution_is_unambiguous_and_correct(self):
        # Distinct equal-length start strings cannot both prefix one URI
        # (they would have to be identical, which the catalog parser
        # rejects); the resolver's longest-first index therefore always
        # yields one target. This test pins the longest-match outcome.
        from taxonomy_package.models import CatalogRule, PackageMember
        from taxonomy_package.resolver import SchemaResolver
        members = (
            PackageMember("entry.xsd", sha256_bytes(b"e"), 1, True),
            PackageMember("ext/t.xsd", sha256_bytes(b"a"), 1, True),
            PackageMember("base/t.xsd", sha256_bytes(b"b"), 1, True),
        )
        rules = (
            CatalogRule("http://x.example/ns/ext",
                        "xbrl-pkg://package/ext/",
                        "xbrl-pkg://package/ext/", 1),
            CatalogRule("http://x.example/ns",
                        "xbrl-pkg://package/base/",
                        "xbrl-pkg://package/base/", 2),
        )
        payloads = {
            "entry.xsd":
                (f'<?xml version="1.0"?><schema xmlns="{XSD_NS}">'
                 '<import namespace="u"'
                 ' schemaLocation="http://x.example/ns/ext/t.xsd"/>'
                 '<import namespace="v"'
                 ' schemaLocation="http://x.example/ns/t.xsd"/>'
                 '</schema>').encode(),
            "ext/t.xsd": simple_schema().encode(),
            "base/t.xsd": simple_schema().encode(),
        }
        resolver = SchemaResolver(
            package_sha256="a" * 64, members=members,
            catalog_rules=rules, payloads=payloads)
        files, _edges = resolver.resolve(
            ("xbrl-pkg://package/entry.xsd",))
        uris = {f.public_uri for f in files}
        self.assertIn("xbrl-pkg://package/ext/t.xsd", uris)
        self.assertIn("xbrl-pkg://package/base/t.xsd", uris)

    def test_existing_registry_directory_refused_overwrite(self):
        path = self.write_zip("good.zip", self._base_members())
        self.registry.register_zip(path)
        # Constructing a registry with create=True over an existing valid
        # registry must not wipe it.
        again = TaxonomyRegistry(self.registry_path, create=True)
        self.assertEqual(len(again.list_packages()), 1)


class ResolutionSemanticsTest(ServiceTestBase):
    def _register_demo(self, junk_count=200):
        path = os.path.join(self.tmp, "demo.zip")
        fb.build_zip(path, junk_count=junk_count)
        return self.registry.register_zip(path)

    def test_demo_closure_files_edges_and_cycle(self):
        result = self._register_demo()
        batch = self.registry.resolve_entries(result.package_sha256)
        # The 8 schema/linkbase XML members are reachable; the META-INF
        # descriptor/catalog and all 220 junk members stay out of the closure.
        non_meta = [p for p in fb.MEMBERS if not p.startswith("META-INF/")]
        self.assertEqual(batch.file_count, len(non_meta))
        member_paths = {f.member_path for f in batch.files}
        self.assertEqual(member_paths, set(non_meta))
        # Every junk member is absent from the closure.
        for f in batch.files:
            self.assertFalse(f.member_path.startswith("junk/"))
        # One back edge in the cyclic-a <-> cyclic-b pair.
        self.assertEqual(batch.cycle_edge_count, 1)
        cycle_edges = [e for e in batch.edges if e.is_cycle]
        self.assertEqual(len(cycle_edges), 1)
        # Linkbase reference is recorded with its origin.
        lb = [e for e in batch.edges if e.kind == "linkbaseRef"]
        self.assertEqual(len(lb), 1)
        self.assertIn("labels.xml", lb[0].target_uri)

    def test_two_entries_share_one_resolution(self):
        result = self._register_demo()
        digest = result.package_sha256
        ep1 = "xbrl-pkg://package/schemas/ep1.xsd"
        ep2 = "xbrl-pkg://package/schemas/ep2.xsd"
        b1 = self.registry.resolve_entries(digest, (ep1,))
        b2 = self.registry.resolve_entries(digest, (ep2,))
        both = self.registry.resolve_entries(digest, (ep1, ep2))
        types_uri = "xbrl-pkg://package/schemas/base/types.xsd"
        # The shared type schema is one logical URI in every closure.
        for batch in (b1, b2, both):
            uris = [f.public_uri for f in batch.files]
            self.assertEqual(uris.count(types_uri), 1)
        # Same entry set, same batch (no duplicate output).
        again = self.registry.resolve_entries(digest, (ep1, ep2))
        self.assertEqual(again.batch_id, both.batch_id)
        # Each distinct file appears exactly once even though two entries
        # import it.
        self.assertEqual(
            len(both.files), len({f.public_uri for f in both.files}))

    def test_unknown_entry_refused(self):
        result = self._register_demo()
        with self.assertRaises(InvocationError):
            self.registry.resolve_entries(
                result.package_sha256,
                ("xbrl-pkg://package/schemas/nope.xsd",))

    def test_output_is_stable_across_runs(self):
        result = self._register_demo()
        first = self.registry.resolve_entries(result.package_sha256)
        # Rebuild the registry from scratch and resolve the same package.
        other_path = os.path.join(self.tmp, "registry2")
        other = TaxonomyRegistry(other_path, create=True)
        zip_path = os.path.join(self.tmp, "demo.zip")
        r2 = other.register_zip(zip_path)
        second = other.resolve_entries(r2.package_sha256)
        self.assertEqual(
            [(f.public_uri, f.member_path) for f in first.files],
            [(f.public_uri, f.member_path) for f in second.files])
        self.assertEqual(
            [(e.source_uri, e.target_uri, e.kind, e.is_cycle)
             for e in first.edges],
            [(e.source_uri, e.target_uri, e.kind, e.is_cycle)
             for e in second.edges])

    def test_cycle_does_not_hang_and_every_file_once(self):
        members = {
            "p/META-INF/taxonomyPackage.xml": descriptor(),
            "p/META-INF/catalog.xml": catalog(
                [("http://x.example/ns", "xbrl-pkg://package/schemas/")]),
            "p/schemas/entry.xsd": simple_schema(
                imports=("http://x.example/ns/a.xsd",)),
            "p/schemas/a.xsd": simple_schema(
                imports=("http://x.example/ns/b.xsd",)),
            "p/schemas/b.xsd": simple_schema(
                imports=("http://x.example/ns/a.xsd",)),
        }
        result = self.registry.register_zip(
            self.write_zip("cyc.zip", members))
        batch = self.registry.resolve_entries(result.package_sha256)
        self.assertEqual(batch.file_count, 3)
        self.assertEqual(batch.cycle_edge_count, 1)

    def test_longest_prefix_wins(self):
        result = self._register_demo()
        batch = self.registry.resolve_entries(result.package_sha256)
        ext = next(f for f in batch.files
                   if f.member_path == "schemas/ext/ext-types.xsd")
        self.assertTrue(ext.public_uri.startswith(
            "xbrl-pkg://package/schemas/ext/"))
        base = next(f for f in batch.files
                    if f.member_path == "schemas/base/types.xsd")
        self.assertTrue(base.public_uri.startswith(
            "xbrl-pkg://package/schemas/base/"))

    def test_xml_base_relative_include_resolves(self):
        result = self._register_demo()
        batch = self.registry.resolve_entries(result.package_sha256)
        paths = {f.member_path for f in batch.files}
        self.assertIn("schemas/base/parts/types-base.xsd", paths)

    def test_query_covers_public_uri_raw_href_and_final_target(self):
        result = self._register_demo(junk_count=3)
        digest = result.package_sha256
        self.registry.resolve_entries(digest)
        # 1. public (pre-rewrite) URI reaches the resolved file via edges
        public = "http://example.com/taxonomy/base/types.xsd"
        answer = self.registry.query_uri(digest, public)
        self.assertTrue(answer["edges"])
        self.assertTrue(
            any(f["member_path"] == "schemas/base/types.xsd"
                for f in answer["resolution_files"]))
        # 2. original raw relative href is queryable
        raw = self.registry.query_uri(digest, "../linkbases/labels.xml")
        self.assertTrue(raw["edges"])
        self.assertTrue(
            any(f["member_path"] == "linkbases/labels.xml"
                for f in raw["resolution_files"]))
        # 3. final target member path
        target = self.registry.query_uri(
            digest, "schemas/cycle/cyclic-a.xsd")
        self.assertIsNotNone(target["member"])
        # 4. logical URI resolves to the member directly
        logical = self.registry.query_uri(
            digest, "xbrl-pkg://package/schemas/ep1.xsd")
        self.assertEqual(logical["resolved_member_path"],
                         "schemas/ep1.xsd")

    def test_xml_base_on_entry_point_and_catalog(self):
        # The entry href resolves against a descriptor-level xml:base, and a
        # relative rewritePrefix resolves against a catalog-level xml:base.
        members = {
            "p/META-INF/taxonomyPackage.xml": f"""<?xml version="1.0"?>
<tp:taxonomyPackage xmlns:tp="{TP_NS}" xml:base="schemas/">
 <tp:identifier>xb-pkg</tp:identifier><tp:name>n</tp:name><tp:version>1</tp:version>
 <tp:entryPoints><tp:entryPoint>
   <tp:entryPointDocument href="entry.xsd"/>
 </tp:entryPoint></tp:entryPoints>
</tp:taxonomyPackage>""",
            "p/META-INF/catalog.xml": f"""<?xml version="1.0"?>
<catalog xmlns="{CAT_NS}" xml:base="../">
 <rewriteURI uriStartString="http://xb/ns" rewritePrefix="schemas/"/>
</catalog>""",
            "p/schemas/entry.xsd":
                f'<?xml version="1.0"?><schema xmlns="{XSD_NS}">'
                '<import namespace="u"'
                ' schemaLocation="http://xb/ns/dep.xsd"/></schema>',
            "p/schemas/dep.xsd":
                f'<?xml version="1.0"?><schema xmlns="{XSD_NS}"/>',
        }
        result = self.registry.register_zip(
            self.write_zip("xb.zip", members))
        batch = self.registry.resolve_entries(result.package_sha256)
        self.assertEqual(
            {f.member_path for f in batch.files},
            {"schemas/entry.xsd", "schemas/dep.xsd"})

    def test_same_content_different_uris_keep_separate_logical_sources(self):        # Two distinct public URIs mapping to identical bytes: both stay.
        shared = simple_schema()
        members = {
            "p/META-INF/taxonomyPackage.xml":
                descriptor(entries=("schemas/entry.xsd",)),
            "p/META-INF/catalog.xml": catalog([
                ("http://a.example/ns", "xbrl-pkg://package/schemas/a/"),
                ("http://b.example/ns", "xbrl-pkg://package/schemas/b/"),
            ]),
            "p/schemas/entry.xsd":
                f'<?xml version="1.0"?><schema xmlns="{XSD_NS}">'
                '<import namespace="u" schemaLocation="http://a.example/ns/s.xsd"/>'
                '<import namespace="u" schemaLocation="http://b.example/ns/s.xsd"/>'
                '</schema>',
            "p/schemas/a/s.xsd": shared,
            "p/schemas/b/s.xsd": shared,
        }
        result = self.registry.register_zip(
            self.write_zip("dup.zip", members))
        batch = self.registry.resolve_entries(result.package_sha256)
        s_files = [f for f in batch.files if f.member_path.endswith("s.xsd")]
        self.assertEqual(len(s_files), 2)
        self.assertNotEqual(s_files[0].public_uri, s_files[1].public_uri)
        self.assertEqual(s_files[0].member_sha256, s_files[1].member_sha256)


class VerifyTest(ServiceTestBase):
    def _register(self):
        path = os.path.join(self.tmp, "demo.zip")
        fb.build_zip(path, junk_count=5)
        result = self.registry.register_zip(path)
        self.registry.resolve_entries(result.package_sha256)
        return result

    def test_verify_clean(self):
        self._register()
        report = self.registry.verify()
        self.assertTrue(report.ok, report.problems)
        self.assertGreater(report.members_verified, 0)
        self.assertEqual(report.batches_verified, 1)

    def test_missing_member_detected(self):
        result = self._register()
        # Delete one blob on disk.
        blobs = self.registry.store.iter_blob_paths(result.package_sha256)
        os.remove(blobs[0])
        report = self.registry.verify()
        self.assertFalse(report.ok)
        self.assertTrue(any("missing" in p for p in report.problems))

    def test_changed_bytes_detected_with_member_name(self):
        result = self._register()
        target_member = "schemas/ep1.xsd"
        detail = self.registry.show_package(result.package_sha256)
        row = next(m for m in detail["members"]
                   if m["member_path"] == target_member)
        blob = self.registry.store.blob_path(
            result.package_sha256, row["blob_digest"])
        with open(blob, "wb") as handle:
            handle.write(b"tampered bytes")
        report = self.registry.verify()
        self.assertFalse(report.ok)
        self.assertTrue(any(target_member in p for p in report.problems))

    def test_no_sidecar_files_in_registry(self):
        self._register()
        for root, _dirs, files in os.walk(self.registry_path):
            for fn in files:
                self.assertFalse(
                    fn.endswith(("-wal", "-shm", "-journal")),
                    f"unexpected sidecar {fn}")


if __name__ == "__main__":
    unittest.main()
