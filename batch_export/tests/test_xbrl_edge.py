"""Neighbourhood tests: things the demo fixtures do not exercise directly."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_ingest import ConceptSpec, XbrlIngestor  # noqa: E402
from xbrl_ingest.errors import (  # noqa: E402
    IngestError,
    InvalidValueError,
    SourceLocation,
    XbrlXmlError,
)
from xbrl_ingest.qname import canonical_decimal_text, parse_decimal, validate_date  # noqa: E402
from xbrl_ingest.models import ContextCoordinate, Entity, Period, UnitCoordinate, UnitMeasureTerm  # noqa: E402

NS = "http://example.com/t"
P = "<?xml version='1.0'?>\n"


def inst(body, extra_ns="", root_attrs=""):
    return (
        P + f"<xbrl xmlns='http://www.xbrl.org/2003/instance' xmlns:t='{NS}'"
        + extra_ns + root_attrs + ">" + body + "</xbrl>"
    ).encode("utf-8")


CTX = """<context id="c"><entity><identifier scheme="s">{id}</identifier></entity>
<period><instant>2024-12-31</instant></period></context>"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "db.sqlite")
        self.ing = XbrlIngestor(self.db, [
            ConceptSpec(NS, "A", "decimal"),
            ConceptSpec(NS, "L", "text"),
            ConceptSpec(NS, "E", "decimal"),
        ])
        self.addCleanup(self.ing.close)

    def load(self, body, **kw) -> bytes:
        path = os.path.join(self.tmp, "case.xml")
        with open(path, "wb") as fh:
            fh.write(inst(body, **kw))
        return self.ing.ingest_file(path)

    def kind(self, body, **kw):
        with self.assertRaises(IngestError) as caught:
            self.load(body, **kw)
        return caught.exception


class TestStructure(Base):
    def test_nested_fact_in_tuple_positioned_in_unknown_namespace_is_refused(self):
        body = (
            "<context id='c'><entity><identifier scheme='s'>e</identifier></entity>"
            "<period><instant>2024-12-31</instant></period></context>"
            "<unit id='u'><measure>t:USD</measure></unit>"
            "<x:Nope xmlns:x='urn:other'/>"
        )
        exc = self.kind(body)
        self.assertIn("urn:other", str(exc))
        self.assertRegex(str(exc), r":\d+:\d+:")

    def test_concept_without_context_ref_refused(self):
        body = CTX + "<t:A unitRef='u' decimals='2'>1</t:A>"
        exc = self.kind(body)
        self.assertIn("contextRef", str(exc))

    def test_decimal_without_unit_ref_refused(self):
        body = CTX + "<unit id='u'><measure>t:USD</measure></unit><t:A contextRef='c' decimals='2'>1</t:A>"
        exc = self.kind(body)
        self.assertIn("unitRef", str(exc))

    def test_missing_unit_target_refused_even_if_context_ok(self):
        body = CTX + "<t:A contextRef='c' unitRef='u9' decimals='2'>1</t:A>"
        exc = self.kind(body)
        self.assertIn("u9", str(exc))

    def test_document_that_is_not_wellformed_reports_position(self):
        path = os.path.join(self.tmp, "broken.xml")
        with open(path, "wb") as fh:
            fh.write(b"<xbrl xmlns='http://www.xbrl.org/2003/instance'>&bogus;</xbrl>")
        with self.assertRaises(XbrlXmlError) as caught:
            self.ing.ingest_file(path)
        self.assertIn(path, str(caught.exception))
        self.assertRegex(str(caught.exception), r":\d+:\d+:")

    def test_external_entity_is_not_fetched(self):
        path = os.path.join(self.tmp, "ext.xml")
        with open(path, "wb") as fh:
            fh.write(b'<!DOCTYPE xbrl [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                     b"<xbrl xmlns='http://www.xbrl.org/2003/instance'>&x;</xbrl>")
        with self.assertRaises(XbrlXmlError):
            self.ing.ingest_file(path)
        self.assertEqual(self.ing.store.fact_row_count(), 0)

    def test_scenario_and_segment_both_work(self):
        xbrli = "http://www.xbrl.org/2003/instance"
        xbrldi = "http://xbrl.org/2006/xbrldi"
        body = (
            f"<context id='c'><entity xmlns='{xbrli}'>"
            "<identifier scheme='s'>e</identifier>"
            f"<segment xmlns='{xbrli}'><explicitMember xmlns='{xbrldi}'"
            " dimension='m:d'>m:EMEA</explicitMember></segment>"
            "</entity>"
            f"<period xmlns='{xbrli}'><instant>2024-12-31</instant></period>"
            f"<scenario xmlns='{xbrli}'><explicitMember xmlns='{xbrldi}'"
            " dimension='m:d2'>m:X</explicitMember></scenario>"
            "</context>"
        )
        report = self.load(body, extra_ns=" xmlns:m='urn:m'")
        contexts = self.ing.contexts_for_source(report.source_id)
        self.assertEqual(len(contexts), 1)
        dims = contexts[0][1].dimensions
        self.assertEqual([d for d, _ in dims], ["{urn:m}d", "{urn:m}d2"])
        self.assertEqual([m for _, m in dims], ["{urn:m}EMEA", "{urn:m}X"])

    def test_prefix_rebinding_changes_concept_member_and_measure_identity(self):
        body = (
            "<context id='c'><entity><identifier scheme='s'>e</identifier></entity>"
            "<period><instant>2024-12-31</instant></period></context>"
            "<unit id='u'><measure>p:USD</measure></unit>"
            # Concept prefix p is bound to the *dictionary* namespace at the root,
            # so this fact is accepted.
            "<p:A contextRef='c' unitRef='u' decimals='2'>1</p:A>"
        )
        # Root binds p to the concept namespace; inner measure text uses p too.
        report = self.load(body, extra_ns=" xmlns:p='%s'" % NS)
        fact = self.ing.facts_for_source(report.source_id)[0]
        self.assertEqual(fact.concept, "{" + NS + "}A")
        self.assertEqual(fact.coordinate.unit.numerator[0].measure, "{" + NS + "}USD")

    def test_member_prefix_rebound_locally_changes_dimension(self):
        xbrli = "http://www.xbrl.org/2003/instance"
        xbrldi = "http://xbrl.org/2006/xbrldi"
        body = (
            f"<context id='c'><entity xmlns='{xbrli}'>"
            "<identifier scheme='s'>e</identifier>"
            f"<segment xmlns='{xbrli}' xmlns:m='urn:first'>"
            f"<explicitMember xmlns='{xbrldi}' dimension='m:Region'>m:EMEA</explicitMember>"
            f"<explicitMember xmlns='{xbrldi}' xmlns:m='urn:second'"
            " dimension='m:Region'>m:EMEA</explicitMember>"
            "</segment></entity>"
            f"<period xmlns='{xbrli}'><instant>2024-12-31</instant></period></context>"
        )
        report = self.load(body)
        dims = self.ing.contexts_for_source(report.source_id)[0][1].dimensions
        # Same written prefix, two bindings: two distinct dimensions.
        self.assertEqual(sorted(d for d, _ in dims), ["{urn:first}Region", "{urn:second}Region"])

    def test_non_dimension_content_in_segment_refused(self):
        body = (
            "<context id='c'><entity xmlns='http://www.xbrl.org/2003/instance'>"
            "<identifier scheme='s'>e</identifier>"
            "<segment xmlns='http://www.xbrl.org/2003/instance'>"
            "<x:note xmlns:x='urn:x'>hello</x:note></segment></entity>"
            "<period><instant>2024-12-31</instant></period></context>"
        )
        exc = self.kind(body)
        self.assertIn("unsupported", str(exc).lower())

    def test_single_measure_and_product_units(self):
        report = self.load(CTX + "<unit id='u'><measure>t:USD</measure></unit>"
                           + "<t:A contextRef='c' unitRef='u' decimals='2'>1</t:A>")
        fact = self.ing.facts_for_source(report.source_id)[0]
        self.assertEqual(tuple(t.measure for t in fact.coordinate.unit.numerator),
                         ("{" + NS + "}USD",))

    def test_repeated_measure_count_is_preserved(self):
        report = self.load(
            "<context id='c'><entity><identifier scheme='s'>e</identifier></entity>"
            "<period><instant>2024-12-31</instant></period></context>"
            "<unit id='u'><measure>t:USD</measure><measure>t:USD</measure></unit>"
            "<t:A contextRef='c' unitRef='u' decimals='2'>1</t:A>")
        unit = self.ing.facts_for_source(report.source_id)[0].coordinate.unit
        self.assertEqual([(t.measure, t.count) for t in unit.numerator],
                         [("{" + NS + "}USD", 2)])

    def test_unit_id_is_not_a_coordinate(self):
        body = (
            "<context id='c'><entity><identifier scheme='s'>e</identifier></entity>"
            "<period><instant>2024-12-31</instant></period></context>"
            "<unit id='u-one'><measure>t:USD</measure></unit>"
            "<t:A contextRef='c' unitRef='u-one' decimals='2'>1</t:A>"
        )
        r1 = self.load(body, root_attrs=" xmlns:t2='http://xbrl.org/2006/xbrldi'")
        body2 = body.replace("u-one", "u-two")
        path2 = os.path.join(self.tmp, "case2.xml")
        with open(path2, "wb") as fh:
            fh.write(inst(body2))
        r2 = self.ing.ingest_file(path2)
        results = self.ing.query(concept="{" + NS + "}A")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0].candidates), 2)
        self.assertEqual(results[0].status, "single")

    def test_elements_in_default_namespace_only_if_xbrli_else_refused(self):
        body = CTX + "<t:A contextRef='c' unitRef='u' decimals='2'>1</t:A>"
        body = body.replace("<context", "<context xmlns:q='urn:q'")
        # Add an element in the *concept* namespace nested in a context.
        exc = self.kind(
            "<context id='c'><entity><identifier scheme='s'>e</identifier></entity>"
            "<period><instant>2024-12-31</instant></period>"
            "<t:Weird/></context>"
        )
        self.assertIn("Weird", str(exc))


class TestDecimalRules(Base):
    def test_decimals_lexical_rules(self):
        report = self.load(
            CTX + "<unit id='u'><measure>t:USD</measure></unit>"
            "<t:A contextRef='c' unitRef='u' decimals='+3'>1</t:A>"
            "<t:A contextRef='c' unitRef='u' decimals='-2'>2</t:A>"
            "<t:A contextRef='c' unitRef='u' decimals='INF'>3</t:A>")
        values = [f.decimals for f in self.ing.facts_for_source(report.source_id)]
        self.assertEqual(values, ["+3", "-2", "INF"])
        for bad in ("inf", "NAN", "2.5", "", "1e3"):
            with self.assertRaises(InvalidValueError):
                validate_decimals_ = __import__(
                    "xbrl_ingest.qname", fromlist=["validate_decimals"]).validate_decimals(
                    bad, SourceLocation("x", 1, 1))

    def test_numeric_lexical_rejects_float_only_forms(self):
        for bad in ("NaN", "Infinity", "1,000", "1_000", "", "  "):
            with self.assertRaises(InvalidValueError):
                parse_decimal(bad, SourceLocation("x", 1, 1))
        for good in ("1", "+1", ".5", "5.", "1E+3", "-0", "1e-9"):
            parse_decimal(good, SourceLocation("x", 1, 1))

    def test_canonical_decimal_ignores_scale_but_not_value(self):
        self.assertEqual(canonical_decimal_text(parse_decimal("1.0", SourceLocation("x", 1, 1))), "1")
        self.assertEqual(canonical_decimal_text(parse_decimal("1.00", SourceLocation("x", 1, 1))), "1")
        self.assertEqual(canonical_decimal_text(parse_decimal("-0.00", SourceLocation("x", 1, 1))), "0")
        self.assertEqual(canonical_decimal_text(parse_decimal("1E+2", SourceLocation("x", 1, 1))), "100")
        self.assertEqual(canonical_decimal_text(parse_decimal("1E-7", SourceLocation("x", 1, 1))), "0.0000001")
        self.assertNotEqual(
            canonical_decimal_text(parse_decimal("0.00000000000000000000001", SourceLocation("x", 1, 1))), "0"
        )

    def test_decimal_raw_text_kept_while_schema_whitespace_collapses(self):
        # xsd:decimal has whiteSpace=collapse, so surrounding XML whitespace is
        # legal for the *value*; the stored raw text still shows it.
        report = self.load(
            CTX + "<unit id='u'><measure>t:USD</measure></unit>"
            + "<t:A contextRef='c' unitRef='u' decimals='2'> 1 </t:A>"
        )
        fact = self.ing.facts_for_source(report.source_id)[0]
        self.assertEqual(fact.value_text, " 1 ")
        self.assertEqual(fact.value_canonical, "1")

    def test_char_reference_is_kept_in_text(self):
        report = self.load(
            CTX + "<t:L contextRef='c'>line&#10;break</t:L>"
        )
        value = self.ing.facts_for_source(report.source_id)[0].value_text
        self.assertEqual(value, "line\nbreak")

    def test_literal_crlf_normalised_per_xml_rules(self):
        report = self.load(CTX + "<t:L contextRef='c'>a\r\nb</t:L>")
        value = self.ing.facts_for_source(report.source_id)[0].value_text
        self.assertEqual(value, "a\nb")

    def test_empty_text_fact_is_not_nil(self):
        report = self.load(CTX + "<t:L contextRef='c'></t:L>")
        fact = self.ing.facts_for_source(report.source_id)[0]
        self.assertFalse(fact.nil)
        self.assertEqual(fact.value_text, "")

    def test_nil_text_fact(self):
        report = self.load(
            CTX + "<t:L contextRef='c' xsi:nil='true'"
            " xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'/>"
        )
        fact = self.ing.facts_for_source(report.source_id)[0]
        self.assertTrue(fact.nil)
        self.assertIsNone(fact.value_text)
        self.assertIsNone(fact.coordinate.language)

    def test_identical_and_conflicting_nil(self):
        self.load(CTX + "<t:A contextRef='c' unitRef='u' xsi:nil='true'"
                  " xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'/>"
                  "<unit id='u'><measure>t:USD</measure></unit>")
        results = self.ing.query(concept="{" + NS + "}A")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "single")

    def test_nil_vs_zero_conflict(self):
        self.load(
            "<context id='c'><entity><identifier scheme='s'>e</identifier></entity>"
            "<period><instant>2024-12-31</instant></period></context>"
            "<unit id='u'><measure>t:USD</measure></unit>"
            "<t:A contextRef='c' unitRef='u' decimals='2'>0</t:A>"
        )
        path = os.path.join(self.tmp, "nil2.xml")
        with open(path, "wb") as fh:
            fh.write(inst(
                "<context id='c'><entity><identifier scheme='s'>e</identifier></entity>"
                "<period><instant>2024-12-31</instant></period></context>"
                "<unit id='u'><measure>t:USD</measure></unit>"
                "<t:A contextRef='c' unitRef='u' xsi:nil='true'"
                " xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'/>"))
        self.ing.ingest_file(path)
        results = self.ing.query(concept="{" + NS + "}A")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "conflict")
        self.assertEqual(len(results[0].candidates), 2)


class TestDates(unittest.TestCase):
    def test_legal_and_illegal_dates(self):
        good = ["2024-01-01", "2024-02-29", "2000-02-29", "2023-12-31"]
        bad = ["2024-02-30", "2023-02-29", "1900-02-29", "2024-13-01",
               "2024-00-10", "2024-1-01", "24-01-01", "0000-01-01",
               " 2024-01-01", "2024-01-01 ", "2024-01-01T00:00:00"]
        for value in good:
            validate_date(value, SourceLocation("x", 1, 1), "period")
        for value in bad:
            with self.assertRaises(InvalidValueError):
                validate_date(value, SourceLocation("x", 1, 1), "period")

    def test_context_coordinate_rejects_bad_duration(self):
        with self.assertRaises(ValueError):
            Period(kind="duration", start="2024-12-31", end="2024-01-01")


class TestCoordinateModel(unittest.TestCase):
    def test_dimension_order_and_prefix_irrelevant(self):
        a = ContextCoordinate(
            Entity("s", "e"), Period("instant", instant="2024-12-31"),
            dimensions=(("{u}d1", "{u}m1"), ("{u}d2", "{u}m2")),
        )
        b = ContextCoordinate(
            Entity("s", "e"), Period("instant", instant="2024-12-31"),
            dimensions=(("{u}d2", "{u}m2"), ("{u}d1", "{u}m1")),
        )
        self.assertEqual(a.digest, b.digest)

    def test_entity_scheme_and_period_kind_change_digest(self):
        base = ContextCoordinate(Entity("s", "e"), Period("instant", instant="2024-12-31"))
        other_scheme = ContextCoordinate(Entity("s2", "e"), Period("instant", instant="2024-12-31"))
        self.assertNotEqual(base.digest, other_scheme.digest)
        duration = ContextCoordinate(Entity("s", "e"),
                                     Period("duration", start="2024-01-01", end="2024-12-31"))
        self.assertNotEqual(base.digest, duration.digest)
        self.assertEqual(UnitCoordinate(numerator=(UnitMeasureTerm("{u}USD"),)).digest
                         != UnitCoordinate(numerator=(UnitMeasureTerm("{u}USD", 2),)).digest, True)


if __name__ == "__main__":
    unittest.main()
