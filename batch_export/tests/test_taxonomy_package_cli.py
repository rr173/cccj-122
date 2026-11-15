"""End-to-end CLI tests: argument handling, exit codes and JSON output.

The CLI runs in a subprocess whose working directory is an empty temp
directory, so no planted module from the data directory can shadow the
standard library. lxml lives in the user site in this environment.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(PROJECT, "demo", "taxonomy_package_demo")
sys.path.insert(0, PROJECT)
sys.path.insert(0, DEMO)

import fixture_builder as fb  # noqa: E402


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="tp_cli_")
        # Empty cwd for every subprocess.
        self.cwd = os.path.join(self.work, "cwd")
        os.makedirs(self.cwd)
        self.registry = os.path.join(self.work, "registry")
        self.zip_path = os.path.join(self.work, "demo.zip")
        fb.build_zip(self.zip_path, junk_count=5)
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = PROJECT

    def tearDown(self):
        shutil.rmtree(self.work, True)

    def cli(self, *args, expect=None):
        proc = subprocess.run(
            [sys.executable, "-m", "taxonomy_package.cli", *args],
            cwd=self.cwd, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                f"cli {args!r}: rc={proc.returncode}\n{proc.stderr}")
        return proc

    def register(self):
        proc = self.cli("register", self.registry, self.zip_path,
                        "--json", expect=0)
        return json.loads(proc.stdout)[0]["package_sha256"]


class CliHappyPathTest(CliTestBase):
    def test_init_register_list_show_resolve_verify_query(self):
        self.cli("init", self.registry, expect=0)
        digest = self.register()

        proc = self.cli("list", self.registry, "--json", expect=0)
        listing = json.loads(proc.stdout)
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["package_sha256"], digest)

        proc = self.cli("show", self.registry, "--digest", digest,
                        "--json", expect=0)
        detail = json.loads(proc.stdout)
        self.assertEqual(detail["meta"]["identifier"],
                         "com.example.demo-taxonomy")
        self.assertEqual(len(detail["entries"]), 2)

        proc = self.cli("resolve", self.registry, "--digest", digest,
                        "--json", expect=0)
        batch = json.loads(proc.stdout)
        self.assertEqual(batch["cycle_edge_count"], 1)
        self.assertGreater(batch["file_count"], 0)
        # Re-resolving returns the same batch id.
        again = json.loads(self.cli(
            "resolve", self.registry, "--digest", digest,
            "--json", expect=0).stdout)
        self.assertEqual(again["batch_id"], batch["batch_id"])

        # Re-register the identical ZIP: reused, no new copy.
        repeat = json.loads(self.cli(
            "register", self.registry, self.zip_path,
            "--json", expect=0).stdout)[0]
        self.assertTrue(repeat["reused"])

        proc = self.cli("verify", self.registry, "--json", expect=0)
        report = json.loads(proc.stdout)
        self.assertTrue(report["ok"])

        uri = "xbrl-pkg://package/schemas/ep1.xsd"
        proc = self.cli("query", self.registry, "--digest", digest,
                        "--uri", uri, "--json", expect=0)
        answer = json.loads(proc.stdout)
        self.assertEqual(answer["member"]["member_path"],
                         "schemas/ep1.xsd")

    def test_human_output_resolves_ok(self):
        digest = self.register()
        proc = self.cli("resolve", self.registry, "--digest", digest,
                        expect=0)
        self.assertIn("dependency edges", proc.stdout)


class CliFailureTest(CliTestBase):
    def test_bad_zip_exits_package_code(self):
        bad = os.path.join(self.work, "bad.zip")
        with open(bad, "wb") as handle:
            handle.write(b"not a zip")
        proc = self.cli("register", self.registry, bad)
        self.assertEqual(proc.returncode, 4)
        self.assertIn("package error", proc.stderr)

    def test_unknown_digest_exits_invocation_code(self):
        self.register()
        proc = self.cli("show", self.registry,
                        "--digest", "0" * 64)
        self.assertEqual(proc.returncode, 2)

    def test_unknown_batch_exits_invocation_code(self):
        digest = self.register()
        self.cli("resolve", self.registry, "--digest", digest, expect=0)
        proc = self.cli("resolve", self.registry, "--digest", digest,
                        "--batch", "99")
        self.assertEqual(proc.returncode, 2)

    def test_verify_detects_tamper_with_exit_3(self):
        digest = self.register()
        self.cli("resolve", self.registry, "--digest", digest, expect=0)
        # Locate and corrupt one member blob.
        from taxonomy_package.store import TaxonomyStore
        store = TaxonomyStore(self.registry)
        blobs = store.iter_blob_paths(digest)
        with open(blobs[0], "wb") as handle:
            handle.write(b"changed")
        proc = self.cli("verify", self.registry, "--json")
        self.assertEqual(proc.returncode, 3)
        report = json.loads(proc.stdout)
        self.assertFalse(report["ok"])
        self.assertTrue(report["problems"])

    def test_resolve_failure_exits_4_and_persists_nothing(self):
        # Build a package whose entry imports a missing schema.
        import io
        import zipfile
        CAT = "urn:oasis:names:tc:entity:xmlns:xml:catalog"
        TP = "http://xbrl.org/2016/taxonomy-package"
        XSD = "http://www.w3.org/2001/XMLSchema"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("p/META-INF/taxonomyPackage.xml", f"""<?xml version="1.0"?>
<tp:taxonomyPackage xmlns:tp="{TP}">
  <tp:identifier>i</tp:identifier><tp:name>n</tp:name><tp:version>v</tp:version>
  <tp:entryPoints><tp:entryPoint>
    <tp:entryPointDocument href="schemas/entry.xsd"/>
  </tp:entryPoint></tp:entryPoints>
</tp:taxonomyPackage>""")
            z.writestr("p/META-INF/catalog.xml", f"""<?xml version="1.0"?>
<catalog xmlns="{CAT}">
  <rewriteURI uriStartString="http://x.example/ns"
              rewritePrefix="xbrl-pkg://package/schemas/"/>
</catalog>""")
            z.writestr("p/schemas/entry.xsd", f"""<?xml version="1.0"?>
<schema xmlns="{XSD}">
  <import namespace="u"
         schemaLocation="http://x.example/ns/ghost.xsd"/>
</schema>""")
        missing_zip = os.path.join(self.work, "missing.zip")
        with open(missing_zip, "wb") as handle:
            handle.write(buf.getvalue())
        digest = json.loads(self.cli(
            "register", self.registry, missing_zip,
            "--json", expect=0).stdout)[0]["package_sha256"]
        proc = self.cli("resolve", self.registry, "--digest", digest)
        self.assertEqual(proc.returncode, 4)
        self.assertIn("ghost.xsd", proc.stderr)
        # No batch rows persisted.
        detail = json.loads(self.cli(
            "show", self.registry, "--digest", digest,
            "--json", expect=0).stdout)
        self.assertEqual(detail["batches"], [])


if __name__ == "__main__":
    unittest.main()
