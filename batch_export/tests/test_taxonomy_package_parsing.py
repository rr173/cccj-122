"""Unit tests for ZIP safety, descriptor/catalog parsing and URI handling."""
import io
import os
import stat
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "demo", "taxonomy_package_demo"))

from taxonomy_package.errors import (  # noqa: E402
    CatalogConflictError,
    PackageFormatError,
)
from taxonomy_package.uriutil import (  # noqa: E402
    apply_rewrite,
    assert_logical_uri_contained,
    join_uri,
    logical_uri_to_member_path,
    member_logical_uri,
    normalize_member_path,
)
from taxonomy_package.zippkg import load_package_zip  # noqa: E402
from taxonomy_package.xmlutil import parse_xml_bytes  # noqa: E402
from taxonomy_package.catalogdoc import parse_catalog_document  # noqa: E402
from taxonomy_package.descriptor import parse_descriptor_document  # noqa: E402
from taxonomy_package.models import CATALOG_NS, TP_NS  # noqa: E402

CAT_BASE = "xbrl-pkg://package/META-INF/catalog.xml"
DESC_BASE = "xbrl-pkg://package/META-INF/taxonomyPackage.xml"
ROOT_BASE = "xbrl-pkg://package/"


def write_zip(path: str, members: dict, *, symlinks=()) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            if name in symlinks:
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
            else:
                archive.writestr(name, data)


def minimal_package(extra: dict | None = None,
                    *, catalog=None, descriptor=None) -> dict:
    members = {
        "pkg/META-INF/taxonomyPackage.xml": descriptor or f"""<?xml version="1.0"?>
<tp:taxonomyPackage xmlns:tp="{TP_NS}">
  <tp:identifier>id-1</tp:identifier>
  <tp:name>Name</tp:name>
  <tp:version>1.0</tp:version>
  <tp:entryPoints>
    <tp:entryPoint><tp:entryPointDocument href="schemas/a.xsd"/></tp:entryPoint>
  </tp:entryPoints>
</tp:taxonomyPackage>""",
        "pkg/META-INF/catalog.xml": catalog or f"""<?xml version="1.0"?>
<catalog xmlns="{CATALOG_NS}">
  <rewriteURI uriStartString="http://x.example/ns"
              rewritePrefix="xbrl-pkg://package/schemas/"/>
</catalog>""",
        "pkg/schemas/a.xsd": '<?xml version="1.0"?>'
                             '<schema xmlns="http://www.w3.org/2001/XMLSchema"/>',
    }
    if extra:
        members.update({f"pkg/{k}": v for k, v in extra.items()})
    return members


class UriTest(unittest.TestCase):
    def test_package_relative_join_file_and_directory_bases(self):
        base = "xbrl-pkg://package/schemas/base/types.xsd"
        # "parts" without trailing slash is itself a file-style reference;
        # the next relative ref replaces it in the same directory.
        self.assertEqual(
            join_uri(join_uri(base, "parts"), "t.xsd"),
            "xbrl-pkg://package/schemas/base/t.xsd")
        # trailing slash: directory base preserved across the chain
        self.assertEqual(
            join_uri(join_uri(base, "parts/"), "t.xsd"),
            "xbrl-pkg://package/schemas/base/parts/t.xsd")

    def test_parent_segment_join_normalises(self):
        base = "xbrl-pkg://package/schemas/a.xsd"
        self.assertEqual(
            join_uri(base, "../linkbases/l.xml"),
            "xbrl-pkg://package/linkbases/l.xml")

    def test_root_level_join_has_no_double_slash(self):
        self.assertEqual(
            join_uri("xbrl-pkg://package/a.xsd", "b.xsd"),
            "xbrl-pkg://package/b.xsd")
        self.assertEqual(
            join_uri("xbrl-pkg://package/", "b.xsd"),
            "xbrl-pkg://package/b.xsd")

    def test_absolute_ref_replaces_base(self):
        base = "xbrl-pkg://package/schemas/a.xsd"
        self.assertEqual(
            join_uri(base, "xbrl-pkg://package/other/b.xsd"),
            "xbrl-pkg://package/other/b.xsd")

    def test_fragment_is_preserved(self):
        base = "xbrl-pkg://package/schemas/a.xsd"
        self.assertEqual(
            join_uri(base, "b.xsd#frag"),
            "xbrl-pkg://package/schemas/b.xsd#frag")

    def test_public_http_join_uses_rfc3986(self):
        self.assertEqual(
            join_uri("http://x.example/dir/a.xsd", "b.xsd"),
            "http://x.example/dir/b.xsd")

    def test_member_path_rejects_escapes(self):
        for bad in ("../x", "a/../../x", "/abs", "C:/x", "a/../.."):
            with self.assertRaises(PackageFormatError):
                normalize_member_path(bad, where="test")

    def test_percent_encoded_escape_caught(self):
        uri = member_logical_uri("a") + "/%2e%2e/%2e%2e/evil.xsd"
        with self.assertRaises(PackageFormatError):
            logical_uri_to_member_path(uri, where="test")

    def test_containment_helper(self):
        assert_logical_uri_contained(
            "xbrl-pkg://package/schemas/", where="test")
        with self.assertRaises(PackageFormatError):
            assert_logical_uri_contained(
                "xbrl-pkg://package/schemas/../../etc", where="test")

    def test_apply_rewrite_boundary_slash(self):
        self.assertEqual(
            apply_rewrite("http://x.example/ns/t.xsd",
                          "http://x.example/ns",
                          "xbrl-pkg://package/schemas/"),
            "xbrl-pkg://package/schemas/t.xsd")

    def test_apply_rewrite_no_double_slash_without_suffix_slash(self):
        self.assertEqual(
            apply_rewrite("http://x.example/ns-tail",
                          "http://x.example/ns",
                          "xbrl-pkg://package/schemas/"),
            "xbrl-pkg://package/schemas/-tail")


class ZipSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tp_test_")

    def _zip(self, members, **kw):
        path = os.path.join(self.tmp, "p.zip")
        write_zip(path, members, **kw)
        return path

    def test_minimal_package_loads(self):
        pkg, _ = load_package_zip(self._zip(minimal_package()))
        self.assertEqual(pkg.descriptor.identifier, "id-1")
        self.assertEqual(pkg.entry_member_paths, ("schemas/a.xsd",))

    def test_absolute_member_refused(self):
        members = minimal_package()
        members["/pkg/evil.xsd"] = b"x"
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_drive_member_refused(self):
        members = minimal_package()
        members["pkg/C:evil.xsd"] = b"x"
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_parent_member_refused(self):
        members = minimal_package()
        members["pkg/schemas/../evil.xsd"] = b"x"
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_symlink_member_refused(self):
        members = minimal_package()
        members["pkg/link.xsd"] = "a.xsd"
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members, symlinks=("pkg/link.xsd",)))

    def test_duplicate_normalised_path_refused(self):
        members = minimal_package()
        members["pkg/schemas/./a.xsd"] = b"dup"
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_multiple_top_dirs_refused(self):
        members = minimal_package()
        members["other/file.txt"] = b"x"
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_missing_descriptor_refused(self):
        members = minimal_package()
        del members["pkg/META-INF/taxonomyPackage.xml"]
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_missing_catalog_refused(self):
        members = minimal_package()
        del members["pkg/META-INF/catalog.xml"]
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_not_a_zip_refused(self):
        path = os.path.join(self.tmp, "bad.zip")
        with open(path, "wb") as handle:
            handle.write(b"this is not a zip")
        with self.assertRaises(PackageFormatError):
            load_package_zip(path)

    def test_backslash_member_refused(self):
        members = minimal_package()
        members["pkg/schemas\\b.xsd"] = b"x"
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_directory_entry_with_traversal_refused(self):
        members = minimal_package()
        members["pkg/x/../"] = b""
        with self.assertRaises(PackageFormatError):
            load_package_zip(self._zip(members))

    def test_symlink_directory_refused(self):
        members = minimal_package()
        members["pkg/evildir"] = "target"
        with self.assertRaises(PackageFormatError):
            load_package_zip(
                self._zip(members, symlinks=("pkg/evildir",)))


class HardenedXmlTest(unittest.TestCase):
    def test_doctype_refused(self):
        data = (b'<!DOCTYPE r [<!ENTITY x "v">]><r xmlns="x"/>')
        with self.assertRaises(PackageFormatError):
            parse_xml_bytes(data, "test://doc")

    def test_external_entity_refused(self):
        data = (b'<?xml version="1.0"?><!DOCTYPE r ['
                b'<!ENTITY x SYSTEM "file:///etc/hostname">]><r/>')
        with self.assertRaises(PackageFormatError):
            parse_xml_bytes(data, "test://doc")

    def test_plain_xml_parses(self):
        root = parse_xml_bytes(b'<r xmlns="urn:x"><a/></r>', "test://doc")
        self.assertEqual(root.tag, "{urn:x}r")


class CatalogParseTest(unittest.TestCase):
    def test_unknown_rule_refused_not_ignored(self):
        catalog = f"""<?xml version="1.0"?>
<catalog xmlns="{CATALOG_NS}">
  <uri name="http://x.example/u" uri="xbrl-pkg://package/u.xsd"/>
</catalog>"""
        with self.assertRaises(PackageFormatError):
            parse_catalog_document(catalog.encode(), CAT_BASE)

    def test_foreign_element_refused(self):
        catalog = f"""<?xml version="1.0"?>
<catalog xmlns="{CATALOG_NS}" xmlns:o="urn:other">
  <o:weird/>
</catalog>"""
        with self.assertRaises(PackageFormatError):
            parse_catalog_document(catalog.encode(), CAT_BASE)

    def test_same_prefix_different_target_conflict(self):
        catalog = f"""<?xml version="1.0"?>
<catalog xmlns="{CATALOG_NS}">
  <rewriteURI uriStartString="http://x.example/ns"
              rewritePrefix="xbrl-pkg://package/a/"/>
  <rewriteURI uriStartString="http://x.example/ns"
              rewritePrefix="xbrl-pkg://package/b/"/>
</catalog>"""
        with self.assertRaises(CatalogConflictError):
            parse_catalog_document(catalog.encode(), CAT_BASE)

    def test_group_wrapped_rewrite_accepted(self):
        catalog = f"""<?xml version="1.0"?>
<catalog xmlns="{CATALOG_NS}">
  <group prefer="public">
    <rewriteURI uriStartString="http://x.example/ns"
                rewritePrefix="xbrl-pkg://package/s/"/>
  </group>
</catalog>"""
        rules = parse_catalog_document(catalog.encode(), CAT_BASE)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].rewrite_prefix,
                         "xbrl-pkg://package/s/")

    def test_relative_rewrite_prefix_resolved_against_catalog(self):
        catalog = f"""<?xml version="1.0"?>
<catalog xmlns="{CATALOG_NS}">
  <rewriteURI uriStartString="http://x.example/ns"
              rewritePrefix="../schemas/"/>
</catalog>"""
        rules = parse_catalog_document(catalog.encode(), CAT_BASE)
        self.assertEqual(rules[0].rewrite_prefix,
                         "xbrl-pkg://package/schemas/")

    def test_rewrite_prefix_escape_refused(self):
        catalog = f"""<?xml version="1.0"?>
<catalog xmlns="{CATALOG_NS}">
  <rewriteURI uriStartString="http://x.example/ns"
              rewritePrefix="xbrl-pkg://package/../../evil/"/>
</catalog>"""
        with self.assertRaises(PackageFormatError):
            parse_catalog_document(catalog.encode(), CAT_BASE)

    def test_non_absolute_start_string_refused(self):
        catalog = f"""<?xml version="1.0"?>
<catalog xmlns="{CATALOG_NS}">
  <rewriteURI uriStartString="relative/prefix"
              rewritePrefix="xbrl-pkg://package/s/"/>
</catalog>"""
        with self.assertRaises(PackageFormatError):
            parse_catalog_document(catalog.encode(), CAT_BASE)


class DescriptorParseTest(unittest.TestCase):
    def test_prefix_renamed_still_recognised(self):
        descriptor = f"""<?xml version="1.0"?>
<zzz:taxonomyPackage xmlns:zzz="{TP_NS}">
  <zzz:identifier>i</zzz:identifier>
  <zzz:name>n</zzz:name>
  <zzz:version>v</zzz:version>
  <zzz:entryPoints>
    <zzz:entryPoint>
      <zzz:entryPointDocument href="a.xsd"/>
    </zzz:entryPoint>
  </zzz:entryPoints>
</zzz:taxonomyPackage>"""
        desc = parse_descriptor_document(
            descriptor.encode(), DESC_BASE, ROOT_BASE, "d")
        self.assertEqual(desc.identifier, "i")
        self.assertEqual(desc.entry_points[0].href, "a.xsd")

    def test_xml_base_on_entry_document_folded(self):
        descriptor = f"""<?xml version="1.0"?>
<tp:taxonomyPackage xmlns:tp="{TP_NS}"
                    xml:base="subdir/">
  <tp:identifier>i</tp:identifier><tp:name>n</tp:name><tp:version>v</tp:version>
  <tp:entryPoints>
    <tp:entryPoint>
      <tp:entryPointDocument href="a.xsd"/>
    </tp:entryPoint>
  </tp:entryPoints>
</tp:taxonomyPackage>"""
        desc = parse_descriptor_document(
            descriptor.encode(), DESC_BASE, ROOT_BASE, "d")
        self.assertEqual(
            desc.entry_points[0].effective_base,
            "xbrl-pkg://package/subdir/")

    def test_missing_identifier_refused(self):
        descriptor = f"""<?xml version="1.0"?>
<tp:taxonomyPackage xmlns:tp="{TP_NS}">
  <tp:name>n</tp:name><tp:version>v</tp:version>
  <tp:entryPoints>
    <tp:entryPoint><tp:entryPointDocument href="a.xsd"/></tp:entryPoint>
  </tp:entryPoints>
</tp:taxonomyPackage>"""
        with self.assertRaises(PackageFormatError):
            parse_descriptor_document(
                descriptor.encode(), DESC_BASE, ROOT_BASE, "d")

    def test_foreign_root_refused(self):
        with self.assertRaises(PackageFormatError):
            parse_descriptor_document(
                b'<r xmlns="urn:other"/>', DESC_BASE, ROOT_BASE, "d")


if __name__ == "__main__":
    unittest.main()
