"""End-to-end tests for the local XBRL 2.1 ingestion module.

Every expected value comes from demo/expected/demo_expectations.py, which was
written by hand from the XML fixtures. The tests must never derive an expected
value by calling the ingester's canonicalisation code.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_ingest import (  # noqa: E402
    DuplicateIdError,
    InvalidValueError,
    MissingReferenceError,
    UnknownConceptError,
    XbrlIngestor,
    XbrlStore,
    XbrlStructureError,
    XbrlXmlError,
)

DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo")
FIXTURES = os.path.join(DEMO_DIR, "xbrl_fixtures")
sys.path.insert(0, os.path.join(DEMO_DIR, "expected"))
import demo_expectations as X  # noqa: E402


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


class IngestTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xbrl_ingest_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Each test gets its own copy: tests may delete or rewrite fixtures to
        # prove that queries stay readable offline or that a path changed.
        self.fixtures = os.path.join(self.tmp, "fixtures")
        shutil.copytree(FIXTURES, self.fixtures)
        self.db_path = os.path.join(self.tmp, "warehouse.sqlite")
        self.ingestor = XbrlIngestor(self.db_path, self.fixture("concepts.json"))
        self.addCleanup(self.ingestor.close)

    def fixture(self, name: str) -> str:
        return os.path.join(self.fixtures, name)

    def ingest(self, name: str):
        return self.ingestor.ingest_file(self.fixture(name))

    def ingest_all_demos(self):
        reports = {}
        for name in ("demo-A.xml", "demo-B.xml", "demo-C.xml", "demo-D.xml"):
            reports[name] = self.ingest(name)
        return reports


class TestDemoFacts(IngestTestBase):
    def test_source_counts_and_positions_match_handwritten_expectation(self):
        for expected in (X.DEMO_A, X.DEMO_B, X.DEMO_C, X.DEMO_D):
            with self.subTest(path=expected["path"]):
                report = self.ingestor.ingest_file(
                    self.fixture(os.path.basename(expected["path"]))
                )
                self.assertEqual(report.fact_count, expected["fact_count"])
                self.assertEqual(report.context_count, expected["context_count"])
                self.assertEqual(report.unit_count, expected["unit_count"])
                facts = self.ingestor.facts_for_source(report.source_id)
                self.assertEqual(len(facts), expected["fact_count"])
                for row, (position, concept, value_text, nil, language, unit_measures) in zip(
                    facts, expected.get("facts", [])
                ):
                    self.assertEqual(row.position, position)
                    self.assertEqual(row.concept, concept)
                    self.assertEqual(row.nil, nil)
                    self.assertEqual(row.value_text, value_text)
                    self.assertEqual(row.language, language)
                    if unit_measures is None:
                        self.assertIsNone(row.coordinate.unit)
                    else:
                        actual = tuple(t.measure for t in row.coordinate.unit.numerator)
                        want = tuple("{" + X.UNITS + "}" + m for m in unit_measures)
                        self.assertEqual(actual, want)

    def test_decimal_values_are_exact_never_through_float(self):
        report = self.ingest("demo-A.xml")
        by_position = {f.position: f for f in self.ingestor.facts_for_source(report.source_id)}
        # The huge value keeps every digit (float would round it).
        self.assertEqual(
            by_position[5].value_text,
            "1234567890123456789012345678901234567890",
        )
        # 1.0 and 1.00 keep their own texts and scales; they are equal in value.
        self.assertEqual(by_position[3].value_text, "1.0")
        self.assertEqual(by_position[4].value_text, "1.00")
        self.assertEqual(by_position[3].value_canonical, "1")
        self.assertEqual(by_position[4].value_canonical, "1")
        # Tiny value is not flushed to zero.
        self.assertEqual(by_position[2].value_text, "0.000000000001")
        self.assertEqual(by_position[2].value_canonical, "0.000000000001")

    def test_decimals_text_preserved(self):
        report = self.ingest("demo-A.xml")
        by_position = {f.position: f for f in self.ingestor.facts_for_source(report.source_id)}
        self.assertEqual(by_position[1].decimals, "2")
        self.assertEqual(by_position[5].decimals, "INF")
        # nil decimal fact may omit decimals and stays distinct from 0
        self.assertTrue(by_position[6].nil)
        self.assertIsNone(by_position[6].value_text)
        self.assertEqual(by_position[7].value_text, "0")

    def test_nil_is_not_zero_and_not_empty_text(self):
        report = self.ingest("demo-A.xml")
        results = self.ingestor.query(concept_local="Nothing")
        self.assertEqual(len(results), 1)
        nil_fact = results[0].candidates[0]
        self.assertTrue(nil_fact.nil)
        self.assertIsNone(nil_fact.value_text)
        zero = self.ingestor.query(concept_local="Zero")[0]
        self.assertFalse(zero.candidates[0].nil)
        self.assertEqual(zero.candidates[0].value_text, "0")

    def test_text_keeps_whitespace_and_language(self):
        report = self.ingest("demo-A.xml")
        note = self.ingestor.query(concept_local="Note")[0]
        self.assertEqual(note.coordinate.language, "en")
        self.assertEqual(note.candidates[0].value_text, "  retail segment  ")

    def test_text_languages_inherit_and_differ(self):
        self.ingest("demo-D.xml")
        results = self.ingestor.query(concept_local="Note")
        languages = sorted(r.coordinate.language for r in results)
        self.assertEqual(languages, ["de", "en", "fr"])
        en = [r for r in results if r.coordinate.language == "en"][0]
        self.assertEqual(en.status, "conflict")
        self.assertEqual(sorted(f.value_text for f in en.candidates), ["alpha", "beta"])
        fr = [r for r in results if r.coordinate.language == "fr"][0]
        self.assertEqual(fr.status, "single")


class TestBusinessCoordinates(IngestTestBase):
    def test_same_context_with_different_ids_prefixes_and_dimension_order(self):
        self.ingest_all_demos()
        duration = self.ingestor.query(concept=X.REVENUE, period_kind="duration")
        self.assertEqual(len(duration), 1, "context ids/prefixes/order must not split")
        result = duration[0]
        context = result.coordinate.context
        self.assertEqual(context.entity.scheme, X.LEI)
        self.assertEqual(context.entity.identifier, "LEI-001")
        self.assertEqual(context.period.start, "2024-01-01")
        self.assertEqual(context.period.end, "2024-12-31")
        self.assertEqual(context.dimensions, X.DIMS_ALPHA)
        self.assertEqual(result.status, "conflict")
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(
            sorted(f.value_text for f in result.candidates), ["1234.50", "1250.00"]
        )
        # Both source files contribute; neither was dropped.
        self.assertEqual(len(result.sources), 2)

    def test_consistent_duplicates_are_one_coordinate_and_keep_all_sources(self):
        self.ingest_all_demos()
        instant = self.ingestor.query(
            concept=X.REVENUE, period_kind="instant", entity_scheme=X.LEI,
            instant="2024-12-31",
        )
        self.assertEqual(len(instant), 1)
        result = instant[0]
        self.assertEqual(result.status, "single")
        self.assertEqual([f.value_text for f in result.candidates], ["1.0", "1.00", "1.000"])
        self.assertEqual([f.position for f in result.candidates], [3, 4, 2])
        self.assertEqual(result.meaningfully_equal, True)
        self.assertEqual(len({f.source_id for f in result.candidates}), 2)

    def test_reverse_divide_units_do_not_merge(self):
        self.ingest_all_demos()
        tiny = self.ingestor.query(concept=X.TINY)
        self.assertEqual(len(tiny), 1, "same product written in different order must merge")
        unit = tiny[0].coordinate.unit
        self.assertEqual(
            sorted(t.measure for t in unit.numerator), [X.USD, X.SHARES]
        )
        self.assertEqual(unit.denominator, ())
        # demo-A's shares/USD ratio is used by a Revenue fact? No: only A's
        # u-ratio exists, unused. Its coordinate must still be distinct from
        # B's USD/shares unit. Both are declared under the same local id "u-ratio".
        a_report = self.ingestor.source_for_path(self.fixture("demo-A.xml"))
        b_report = self.ingestor.source_for_path(self.fixture("demo-B.xml"))
        a_units = {uid: coord for uid, coord, _ in
                   self.ingestor.units_for_source(a_report.source.source_id)}
        b_units = {uid: coord for uid, coord, _ in
                   self.ingestor.units_for_source(b_report.source.source_id)}
        a_ratio = a_units["u-ratio"]
        b_ratio = b_units["u-ratio"]
        self.assertEqual(tuple(t.measure for t in a_ratio.numerator), (X.SHARES,))
        self.assertEqual(tuple(t.measure for t in a_ratio.denominator), (X.LEGACY_USD,))
        self.assertEqual(tuple(t.measure for t in b_ratio.numerator), (X.USD,))
        self.assertEqual(tuple(t.measure for t in b_ratio.denominator), (X.SHARES,))
        self.assertNotEqual(a_ratio.digest, b_ratio.digest)

    def test_local_prefix_rebinding_changes_measure_identity(self):
        self.ingest("demo-A.xml")
        report = self.ingestor.source_for_path(self.fixture("demo-A.xml"))
        units = {uid: coord for uid, coord, _ in
                 self.ingestor.units_for_source(report.source.source_id)}
        measures = [t.measure for t in units["u-ratio"].numerator] + [
            t.measure for t in units["u-ratio"].denominator
        ]
        # The denominator's "m:" is re-bound locally to the legacy namespace.
        self.assertIn(X.LEGACY_USD, measures)
        self.assertNotIn(X.USD, measures)

    def test_entity_scheme_separates_coordinates(self):
        self.ingest("demo-C.xml")
        results = self.ingestor.query(concept=X.ASSETS)
        self.assertEqual(len(results), 2, "different schemes must not merge")
        schemes = sorted(r.coordinate.context.entity.scheme for r in results)
        self.assertEqual(schemes, [X.INTERNAL, X.LEI])
        for result in results:
            self.assertEqual(result.coordinate.context.entity.identifier, "LEI-001")
            self.assertEqual(len(result.candidates), 1)

    def test_instant_and_duration_are_different_periods(self):
        report = self.ingest("demo-A.xml")
        kinds = {f.coordinate.context.period.kind
                 for f in self.ingestor.facts_for_source(report.source_id)
                 if f.concept == X.REVENUE}
        self.assertEqual(kinds, {"instant", "duration"})

    def test_query_filters_by_dimensions_and_entity(self):
        self.ingest_all_demos()
        by_dim = self.ingestor.query(
            dimensions=[(X.REGION, X.EMEA), (X.PRODUCT_LINE, X.RETAIL)]
        )
        self.assertEqual(len(by_dim), 1)
        self.assertEqual(by_dim[0].status, "conflict")
        by_scheme = self.ingestor.query(entity_scheme=X.INTERNAL)
        self.assertEqual(len(by_scheme), 1)
        self.assertEqual(by_scheme[0].coordinate.concept, X.ASSETS)


class TestFullCoordinateListing(IngestTestBase):
    def test_every_result_matches_handwritten_table(self):
        self.ingest_all_demos()
        results = self.ingestor.query()
        # Three Revenue/Assets/Note coordinates differ only in concept or
        # language, so compare as a sorted list of readable tuples.
        actual = []
        for result in results:
            context = result.coordinate.context
            dims = tuple(
                (name.split("}")[1], member.split("}")[1])
                for name, member in context.dimensions
            )
            unit = None
            if result.coordinate.unit is not None:
                unit = tuple(t.measure.split("}")[1] for t in result.coordinate.unit.numerator)
                if result.coordinate.unit.denominator:
                    unit += ("/",) + tuple(
                        t.measure.split("}")[1] for t in result.coordinate.unit.denominator
                    )
            actual.append((
                result.coordinate.concept.split("}")[1],
                context.entity.scheme,
                context.entity.identifier,
                context.period.kind,
                context.period.instant,
                context.period.start,
                context.period.end,
                dims,
                unit,
                result.coordinate.language,
                result.status,
                len(result.candidates),
            ))
        expected = [
            ("Assets", X.LEI, "LEI-001", "instant", "2024-12-31", None, None, (), ("USD",), None, "single", 1),
            ("Assets", X.INTERNAL, "LEI-001", "instant", "2024-12-31", None, None, (), ("USD",), None, "single", 1),
            ("Huge", X.LEI, "LEI-001", "instant", "2024-12-31", None, None, (), ("USD",), None, "single", 1),
            ("Note", X.LEI, "LEI-001", "instant", "2024-12-31", None, None, (), None, "en", "conflict", 2),
            ("Note", X.LEI, "LEI-002", "instant", "2024-12-31", None, None, (), None, "de", "single", 1),
            ("Note", X.LEI, "LEI-002", "instant", "2024-12-31", None, None, (), None, "en", "conflict", 2),
            ("Note", X.LEI, "LEI-002", "instant", "2024-12-31", None, None, (), None, "fr", "single", 1),
            ("Nothing", X.LEI, "LEI-001", "instant", "2024-12-31", None, None, (), ("USD",), None, "single", 1),
            ("Revenue", X.LEI, "LEI-001", "duration", None, "2024-01-01", "2024-12-31", (("ProductLine", "Retail"), ("Region", "EMEA")), ("USD",), None, "conflict", 2),
            ("Revenue", X.LEI, "LEI-001", "instant", "2024-12-31", None, None, (), ("USD",), None, "single", 3),
            ("Tiny", X.LEI, "LEI-001", "instant", "2024-12-31", None, None, (), ("USD", "shares"), None, "single", 2),
            ("Zero", X.LEI, "LEI-001", "instant", "2024-12-31", None, None, (), ("USD",), None, "single", 2),
        ]
        self.assertEqual(sorted(actual), sorted(expected))

class TestStoreLifecycle(IngestTestBase):
    def test_same_bytes_at_new_path_adds_no_facts(self):
        first = self.ingest("demo-A.xml")
        second = self.ingest("demo-A.xml")
        self.assertEqual(second.outcome, "already_ingested")
        self.assertEqual(second.source_id, first.source_id)
        self.assertEqual(self.ingestor.store.fact_row_count(), X.DEMO_A["fact_count"])

        moved = os.path.join(self.tmp, "moved-elsewhere.xml")
        shutil.copy(self.fixture("demo-A.xml"), moved)
        third = self.ingestor.ingest_file(moved)
        self.assertEqual(third.outcome, "already_ingested")
        self.assertEqual(self.ingestor.store.fact_row_count(), X.DEMO_A["fact_count"])
        self.assertEqual(self.ingestor.store.source_count(), 1)

    def test_same_path_new_content_is_another_source(self):
        first = self.ingest("demo-A.xml")
        target = os.path.join(self.tmp, "sliding.xml")
        shutil.copy(self.fixture("demo-A.xml"), target)
        r1 = self.ingestor.ingest_file(target)
        with open(target, "wb") as fh:
            with open(self.fixture("demo-B.xml"), "rb") as source:
                fh.write(source.read())
        r2 = self.ingestor.ingest_file(target)
        self.assertNotEqual(r1.source_id, r2.source_id)
        self.assertEqual(r2.outcome, "new-source-same-path")
        # old source remains queryable, flagged as a warning
        self.assertTrue(r2.warnings)
        self.assertEqual(self.ingestor.store.source_count(), 2)
        self.assertEqual(
            self.ingestor.store.fact_row_count(),
            X.DEMO_A["fact_count"] + X.DEMO_B["fact_count"],
        )

    def test_file_digest_and_byte_size_are_recorded(self):
        import hashlib
        report = self.ingest("demo-A.xml")
        with open(self.fixture("demo-A.xml"), "rb") as source:
            raw = source.read()
        self.assertEqual(report.content_sha256, hashlib.sha256(raw).hexdigest())
        stored = self.ingestor.source_for_path(self.fixture("demo-A.xml"))
        self.assertEqual(stored.source.content_sha256, report.content_sha256)
        self.assertEqual(stored.source.byte_size, len(raw))

    def test_text_search_returns_whole_coordinate_with_all_candidates(self):
        self.ingest_all_demos()
        exact = self.ingestor.search_text("retail segment", exact=True)
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0].status, "conflict")
        self.assertEqual(len(exact[0].candidates), 2)
        substring = self.ingestor.search_text("retail")
        self.assertEqual(len(substring), 1)
        self.assertEqual(len(substring[0].candidates), 2)
        self.assertEqual(self.ingestor.search_text("no such text"), [])

    def test_source_path_filter(self):
        self.ingest_all_demos()
        results = self.ingestor.query(source_path=self.fixture("demo-C.xml"))
        self.assertEqual(len(results), 2)
        self.assertEqual(self.ingestor.query(source_path=self.tmp), [])

    def test_trace_returns_original_occurrence(self):
        self.ingest_all_demos()
        report = self.ingestor.source_for_path(self.fixture("demo-A.xml"))
        fact = self.ingestor.trace_fact(report.source.source_id, 1)
        self.assertEqual(fact.concept, X.REVENUE)
        self.assertEqual(fact.value_text, "1234.50")
        self.assertEqual(fact.context_id_in_source, "c-alpha")
        self.assertEqual(fact.unit_id_in_source, "u-money")
        self.assertEqual(fact.location.path, self.fixture("demo-A.xml"))
        self.assertGreater(fact.location.line, 1)
        self.assertIsNone(self.ingestor.trace_fact(report.source.source_id, 999))

    def test_persisted_value_columns_are_readable_directly(self):
        self.ingest("demo-A.xml")
        report = self.ingestor.source_for_path(self.fixture("demo-A.xml"))
        huge = self.ingestor.trace_fact(report.source.source_id, 5)
        columns = self.ingestor.store.stored_fact_columns(huge.fact_id)
        self.assertEqual(
            columns["value_text"],
            "1234567890123456789012345678901234567890",
        )
        self.assertEqual(columns["decimals"], "INF")
        self.assertEqual(columns["nil"], 0)
        nil_fact = self.ingestor.trace_fact(report.source.source_id, 6)
        nil_columns = self.ingestor.store.stored_fact_columns(nil_fact.fact_id)
        self.assertEqual(nil_columns["nil"], 1)
        self.assertIsNone(nil_columns["value_text"])

    def test_query_and_trace_work_after_files_are_deleted(self):
        self.ingest_all_demos()
        snapshot = [
            (r.coordinate.canonical_text(), r.status, [f.value_text for f in r.candidates])
            for r in self.ingestor.query()
        ]
        source_ids = [s.source.source_id for s in self.ingestor.sources()]
        for name in os.listdir(self.fixtures):
            if name.endswith(".xml"):
                os.remove(os.path.join(self.fixtures, name))
        for sid in source_ids:
            for fact in self.ingestor.facts_for_source(sid):
                self.assertIsNotNone(fact.coordinate)
                self.assertIsNotNone(fact.value_text if not fact.nil else "nil")
        reopened = XbrlIngestor(self.db_path, self.fixture("concepts.json"))
        self.addCleanup(reopened.close)
        after = [
            (r.coordinate.canonical_text(), r.status, [f.value_text for f in r.candidates])
            for r in reopened.query()
        ]
        self.assertEqual(after, snapshot)


class TestWholeFileRollback(IngestTestBase):
    def _counts(self):
        return (self.ingestor.store.source_count(), self.ingestor.store.fact_row_count())

    def test_every_refused_file_leaves_warehouse_unchanged(self):
        self.ingest_all_demos()
        before = self._counts()
        error_types = {
            "DuplicateIdError": DuplicateIdError,
            "MissingReferenceError": MissingReferenceError,
            "UnknownConceptError": UnknownConceptError,
            "InvalidValueError": InvalidValueError,
            "XbrlStructureError": XbrlStructureError,
            "XbrlXmlError": XbrlXmlError,
        }
        for name, error_name, needle in X.REFUSED:
            with self.subTest(name=name):
                with self.assertRaises(error_types[error_name]) as caught:
                    self.ingest(name)
                message = str(caught.exception)
                self.assertIn(self.fixture(name), message, "must name the file")
                self.assertRegex(message, r":\d+:\d+:", "must name a line:column")
                self.assertIn(needle, message)
                self.assertEqual(self._counts(), before, "no partial facts")

    def test_second_file_failure_does_not_affect_first(self):
        good = self.ingest("demo-C.xml")
        before = self._counts()
        with self.assertRaises(MissingReferenceError):
            self.ingest("bad-missing-ref.xml")
        self.assertEqual(self._counts(), before)
        self.assertEqual(
            self.ingestor.source_for_path(self.fixture("demo-C.xml")).source.source_id,
            good.source_id,
        )


class TestIdempotenceAndIsolation(IngestTestBase):
    def test_duplicate_context_id_fails_even_with_identical_coordinate(self):
        # bad-dup-context-id declares two different entities under one id; the
        # file must be refused as a whole rather than keeping the first.
        with self.assertRaises(DuplicateIdError):
            self.ingest("bad-dup-context-id.xml")
        self.assertEqual(self.ingestor.store.fact_row_count(), 0)

    def test_repeated_ingest_is_byte_stable_in_results(self):
        self.ingest_all_demos()
        before = [(r.coordinate.canonical_text(), r.status, len(r.candidates))
                  for r in self.ingestor.query()]
        self.ingest_all_demos()
        after = [(r.coordinate.canonical_text(), r.status, len(r.candidates))
                 for r in self.ingestor.query()]
        self.assertEqual(before, after)

    def test_database_reopen_sees_same_values(self):
        self.ingest("demo-A.xml")
        coordinate_digest = self.ingestor.query(concept=X.ZERO)[0].coordinate.digest
        self.ingestor.close()
        store = XbrlStore(self.db_path)
        self.addCleanup(store.close)
        result = store.result_for_digest(coordinate_digest)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "single")
        self.assertEqual(result.candidates[0].value_text, "0")
        self.assertEqual(result.candidates[0].coordinate.context.entity.identifier, "LEI-001")


class TestCrashDuringIngest(unittest.TestCase):
    """Kill a real ingestion child after its rows are written, then retry.

    The child writes via the normal atomic path and calls os._exit from the
    testing hook *inside* the open transaction. Reopening the database must
    show the pre-ingest state; re-running the file must complete fully.
    """

    def test_process_exit_rolls_back_and_retry_succeeds(self):
        tmp = tempfile.mkdtemp(prefix="xbrl_ingest_crash_")
        self.addCleanup(shutil.rmtree, tmp, True)
        fixtures = os.path.join(tmp, "fixtures")
        shutil.copytree(FIXTURES, fixtures)
        db_path = os.path.join(tmp, "warehouse.sqlite")
        script = os.path.join(tmp, "crash_child.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "import os, sys\n"
                "sys.path.insert(0, %r)\n"
                "from xbrl_ingest import XbrlIngestor\n"
                "def hook(source_id):\n"
                "    print('WROTE', source_id, flush=True)\n"
                "    os._exit(77)\n"
                "ing = XbrlIngestor(sys.argv[1], sys.argv[2])\n"
                "ing.ingest_file(sys.argv[3], system_exit_hook=hook)\n"
                "print('COMMITTED', flush=True)\n" % (
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                )
            )
        before_store = XbrlStore(db_path)
        before_store.close()
        proc = subprocess.run(
            [sys.executable, "-I", script, db_path,
             os.path.join(fixtures, "concepts.json"),
             os.path.join(fixtures, "demo-A.xml")],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertIn("WROTE", proc.stdout)
        self.assertNotIn("COMMITTED", proc.stdout)

        store = XbrlStore(db_path)
        self.addCleanup(store.close)
        self.assertEqual(store.source_count(), 0)
        self.assertEqual(store.fact_row_count(), 0)
        store.close()

        ing = XbrlIngestor(db_path, os.path.join(fixtures, "concepts.json"))
        self.addCleanup(ing.close)
        report = ing.ingest_file(os.path.join(fixtures, "demo-A.xml"))
        self.assertEqual(report.outcome, "ingested")
        self.assertEqual(report.fact_count, X.DEMO_A["fact_count"])
        self.assertEqual(ing.store.source_count(), 1)
        self.assertEqual(ing.store.fact_row_count(), X.DEMO_A["fact_count"])


if __name__ == "__main__":
    unittest.main()
