"""Catalog and policy validation tests for controlled_query."""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cq_helpers as cq
from controlled_query import (
    ConfigError,
    ControlledQueryService,
    InvocationError,
    load_catalog,
    load_policy,
    parse_catalog,
    parse_policy,
)


class CatalogStructuralTest(unittest.TestCase):
    def base(self):
        return cq.catalog_document()

    def expect_error(self, document, needle):
        with self.assertRaises(ConfigError) as ctx:
            parse_catalog(document)
        self.assertIn(needle, str(ctx.exception))

    def test_valid_document_loads(self):
        catalog = parse_catalog(self.base())
        self.assertEqual(catalog.public_names(), ["fact_lines", "shipment_lines"])
        rel = catalog.relation("FACT_LINES")  # case-insensitive lookup
        self.assertEqual(rel.name, "fact_lines")
        self.assertEqual([f.name for f in rel.fields],
                         ["fact id", "concept", "raw value", "marker"])
        self.assertEqual(len(rel.hops), 1)
        self.assertEqual(rel.terminal_table, "contexts")

    def test_version_required(self):
        doc = self.base()
        doc["version"] = 99
        self.expect_error(doc, "version")

    def test_public_name_must_differ_from_source(self):
        doc = self.base()
        doc["relations"][0]["name"] = "facts"
        self.expect_error(doc, "must differ from its source")

    def test_duplicate_public_names_rejected(self):
        doc = self.base()
        doc["relations"][1]["name"] = "FACT_LINES"  # folds to the first
        self.expect_error(doc, "duplicate public name")

    def test_sqlite_prefix_rejected(self):
        doc = self.base()
        doc["relations"][0]["name"] = "sqlite_hidden"
        self.expect_error(doc, "sqlite_")

    def test_duplicate_field_rejected(self):
        doc = self.base()
        doc["relations"][0]["fields"].append({"name": "Concept"})
        self.expect_error(doc, "duplicate field")

    def test_key_count_mismatch_rejected(self):
        doc = self.base()
        doc["relations"][1]["owner"]["path"][0]["right"] = ["region"]
        self.expect_error(doc, "left key count 2 does not match right key count 1")

    def test_path_cycle_rejected(self):
        doc = self.base()
        path = doc["relations"][1]["owner"]["path"]
        path.append({"table": "shipments", "left": ["ship_id"],
                     "right": ["ship_id"]})
        self.expect_error(doc, "cycles")

    def test_self_hop_is_a_cycle(self):
        doc = self.base()
        doc["relations"][0]["owner"]["path"] = [
            {"table": "facts", "left": ["id"], "right": ["id"]}
        ]
        self.expect_error(doc, "cycles")

    def test_summary_hidden_must_be_published(self):
        doc = self.base()
        doc["relations"][0]["summary_hidden"] = ["no such field"]
        self.expect_error(doc, "not a published field")

    def test_empty_path_means_direct_entity_columns(self):
        doc = self.base()
        doc["relations"][0]["owner"]["path"] = []
        doc["relations"][0]["owner"]["entity"] = {
            "scheme": "entity_scheme", "identifier": "entity_identifier"}
        catalog = parse_catalog(doc)
        self.assertEqual(catalog.relations[0].terminal_table, "facts")


class CatalogSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cq_cat_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "wh.sqlite")
        cq.build_warehouse(self.db)
        self.catalog_path = cq.write_catalog(os.path.join(self.tmp, "c.json"))
        self.policy_path = cq.write_policy(os.path.join(self.tmp, "p.json"))

    def service(self, document):
        path = cq.write_catalog(os.path.join(self.tmp, "c.json"), document)
        return ControlledQueryService(self.db, path, self.policy_path)

    def expect_schema_error(self, document, needle):
        service = self.service(document)
        with self.assertRaises(ConfigError) as ctx:
            service.check_config()
        self.assertIn(needle, str(ctx.exception))

    def test_check_config_ok(self):
        service = self.service(cq.catalog_document())
        report = service.check_config()
        self.assertEqual([r[0] for r in report.relations],
                         ["fact_lines", "shipment_lines"])

    def test_unknown_source_table(self):
        doc = cq.catalog_document()
        doc["relations"][0]["source"] = "no_such_table"
        self.expect_schema_error(doc, "does not exist")

    def test_view_source_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE VIEW v_facts AS SELECT id, concept FROM facts")
        conn.commit()
        conn.close()
        doc = cq.catalog_document()
        doc["relations"][0]["source"] = "v_facts"
        self.expect_schema_error(doc, "view")

    def test_virtual_table_source_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE VIRTUAL TABLE vt USING fts5(content)")
        conn.commit()
        conn.close()
        doc = cq.catalog_document()
        doc["relations"][0]["source"] = "vt"
        self.expect_schema_error(doc, "virtual table")

    def test_unknown_field_column(self):
        doc = cq.catalog_document()
        doc["relations"][0]["fields"][1] = {"name": "nope", "column": "no_col"}
        self.expect_schema_error(doc, "unknown column")

    def test_generated_column_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.execute("ALTER TABLE facts ADD COLUMN gen TEXT GENERATED ALWAYS AS"
                     " (concept || '!') VIRTUAL")
        conn.commit()
        conn.close()
        doc = cq.catalog_document()
        doc["relations"][0]["fields"].append({"name": "gen", "column": "gen"})
        self.expect_schema_error(doc, "generated column")

    def test_non_binary_collation_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.execute("ALTER TABLE facts ADD COLUMN loose TEXT COLLATE NOCASE")
        conn.commit()
        conn.close()
        doc = cq.catalog_document()
        doc["relations"][0]["fields"].append({"name": "loose", "column": "loose"})
        self.expect_schema_error(doc, "BINARY")

    def test_exotic_declared_type_rejected(self):
        conn = sqlite3.connect(self.db)
        conn.execute('ALTER TABLE facts ADD COLUMN odd "WEIRD+TYPE"')
        conn.commit()
        conn.close()
        doc = cq.catalog_document()
        doc["relations"][0]["fields"].append({"name": "odd", "column": "odd"})
        self.expect_schema_error(doc, "declared type")

    def test_public_name_colliding_with_other_table_rejected(self):
        doc = cq.catalog_document()
        doc["relations"][0]["name"] = "contexts"  # a real table, not its source
        self.expect_schema_error(doc, "collides with a physical")

    def test_unknown_hop_table_and_keys(self):
        doc = cq.catalog_document()
        doc["relations"][0]["owner"]["path"][0]["table"] = "nope"
        self.expect_schema_error(doc, "does not exist")
        doc = cq.catalog_document()
        doc["relations"][0]["owner"]["path"][0]["left"] = ["nope"]
        self.expect_schema_error(doc, "unknown left key")
        doc = cq.catalog_document()
        doc["relations"][0]["owner"]["path"][0]["right"] = ["nope"]
        self.expect_schema_error(doc, "unknown right key")

    def test_entity_column_must_exist_and_be_text(self):
        doc = cq.catalog_document()
        doc["relations"][0]["owner"]["entity"]["scheme"] = "nope"
        self.expect_schema_error(doc, "unknown entity scheme column")
        doc = cq.catalog_document()
        # facts.id has INTEGER affinity: not a text entity column.
        doc["relations"][0]["owner"]["path"] = []
        doc["relations"][0]["owner"]["entity"] = {
            "scheme": "concept", "identifier": "id"}
        self.expect_schema_error(doc, "TEXT affinity")


class DataContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cq_data_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "wh.sqlite")
        cq.build_warehouse(self.db)
        self.catalog_path = cq.write_catalog(os.path.join(self.tmp, "c.json"))
        self.policy_path = cq.write_policy(os.path.join(self.tmp, "p.json"))
        self.service = ControlledQueryService(
            self.db, self.catalog_path, self.policy_path)

    def test_duplicate_target_key_fails_then_recovers(self):
        conn = sqlite3.connect(self.db)
        # Break the uniqueness of lanes(region, hub): two rows for EU/H1.
        conn.execute("INSERT INTO lanes VALUES ('EU', 'H1', 'OP2')")
        conn.commit()
        conn.close()
        with self.assertRaises(ConfigError) as ctx:
            self.service.query("alice", sql="SELECT * FROM fact_lines")
        message = str(ctx.exception)
        self.assertIn("shipment_lines", message)
        self.assertIn("not unique", message)
        # The whole query fails even though fact_lines itself is unaffected.
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM lanes WHERE region='EU' AND hub='H1'"
                     " AND op_code='OP2'")
        conn.commit()
        conn.close()
        report = self.service.query("alice", sql="SELECT COUNT(*) AS c FROM fact_lines")
        self.assertEqual(cq.display_rows(report), [("2",)])

    def test_null_target_key_fails(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO lanes VALUES ('EU', NULL, 'OP1')")
        conn.commit()
        conn.close()
        with self.assertRaises(ConfigError) as ctx:
            self.service.query("alice", sql="SELECT * FROM fact_lines")
        self.assertIn("non-null", str(ctx.exception))

    def test_non_text_owner_value_fails(self):
        conn = sqlite3.connect(self.db)
        # TEXT affinity converts numbers to text, but a BLOB value is stored
        # verbatim even in a TEXT column; it is not a text entity identity.
        conn.execute("INSERT INTO operators VALUES ('OP9', x'00ff', 'ACME-7', 'x')")
        conn.execute("INSERT INTO lanes VALUES ('ZZ', 'Z9', 'OP9')")
        conn.execute("INSERT INTO shipments VALUES (9, 'ZZ', 'Z9', 1, 'x', NULL, 'm')")
        conn.commit()
        conn.close()
        with self.assertRaises(ConfigError) as ctx:
            self.service.query("alice", sql="SELECT * FROM shipment_lines")
        self.assertIn("TEXT", str(ctx.exception))


class PolicyTest(unittest.TestCase):
    def test_unknown_identity_is_invocation_error(self):
        policy = parse_policy(cq.policy_document())
        with self.assertRaises(InvocationError):
            policy.identity("mallory")

    def test_resolution_matrix(self):
        policy = parse_policy(cq.policy_document())
        resolve = lambda name: policy.resolve(policy.identity(name))
        self.assertEqual(resolve("alice"), {cq.E1: "full"})
        self.assertEqual(resolve("betty"), {cq.E2: "full"})
        self.assertEqual(resolve("sally"), {cq.E2: "summary"})
        # group overlay: full + summary on the same entity -> summary
        self.assertEqual(resolve("ginny"), {cq.E1: "summary"})
        # deny beats the identity's own full grant
        self.assertEqual(resolve("danny"), {})
        # registered without any grant -> empty map (empty relations)
        self.assertEqual(resolve("nancy"), {})
        self.assertEqual(resolve("mixed"), {cq.E1: "full", cq.E2: "summary"})

    def test_deny_wins_regardless_of_grant_order(self):
        doc = cq.policy_document()
        doc["identities"] = [{"name": "x", "groups": ["g"]}]
        doc["grants"] = [
            {"subject": "g", "scheme": "s", "identifier": "i", "effect": "deny"},
            {"subject": "x", "scheme": "s", "identifier": "i", "effect": "full"},
        ]
        policy = parse_policy(doc)
        self.assertEqual(policy.resolve(policy.identity("x")), {})
        doc["grants"].reverse()
        policy = parse_policy(doc)
        self.assertEqual(policy.resolve(policy.identity("x")), {})

    def test_summary_beats_full_regardless_of_order(self):
        doc = cq.policy_document()
        doc["identities"] = [{"name": "x", "groups": ["g"]}]
        doc["grants"] = [
            {"subject": "x", "scheme": "s", "identifier": "i", "effect": "summary"},
            {"subject": "g", "scheme": "s", "identifier": "i", "effect": "full"},
        ]
        policy = parse_policy(doc)
        self.assertEqual(policy.resolve(policy.identity("x")),
                         {("s", "i"): "summary"})
        doc["grants"].reverse()
        policy = parse_policy(doc)
        self.assertEqual(policy.resolve(policy.identity("x")),
                         {("s", "i"): "summary"})

    def test_scheme_is_part_of_identity(self):
        doc = cq.policy_document()
        doc["identities"] = [{"name": "x", "groups": []}]
        doc["grants"] = [
            {"subject": "x", "scheme": "lei", "identifier": "SAME", "effect": "full"},
        ]
        policy = parse_policy(doc)
        resolved = policy.resolve(policy.identity("x"))
        self.assertEqual(resolved, {("lei", "SAME"): "full"})
        # A different scheme with the same identifier is a different entity.
        self.assertNotIn(("nat", "SAME"), resolved)

    def test_policy_validation(self):
        doc = cq.policy_document()
        doc["identities"].append({"name": "alice", "groups": []})
        with self.assertRaises(ConfigError):
            parse_policy(doc)  # duplicate identity

        doc = cq.policy_document()
        doc["identities"][0]["groups"] = ["alice"]  # identity name as group
        with self.assertRaises(ConfigError):
            parse_policy(doc)

        doc = cq.policy_document()
        doc["grants"][0]["subject"] = "nobody"
        with self.assertRaises(ConfigError):
            parse_policy(doc)  # unknown subject

        doc = cq.policy_document()
        doc["grants"][0]["effect"] = "everything"
        with self.assertRaises(ConfigError):
            parse_policy(doc)  # bad effect

        doc = cq.policy_document()
        doc["grants"][0]["identifier"] = ""
        with self.assertRaises(ConfigError):
            parse_policy(doc)  # empty identifier


if __name__ == "__main__":
    unittest.main()
