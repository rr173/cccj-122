"""Tests for linkbase parsing, mapping validation and network resolution."""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calc_check.errors import LinkbaseError  # noqa: E402
from calc_check.linkbase import (  # noqa: E402
    load_locator_mapping,
    parse_linkbase_file,
)
from calc_check.network import resolve_networks  # noqa: E402
from xbrl_ingest import ConceptSpec  # noqa: E402

NS = "http://example.com/calc"
ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"
CONCEPTS = {
    (NS, "Total"): ConceptSpec(NS, "Total", "decimal"),
    (NS, "A"): ConceptSpec(NS, "A", "decimal"),
    (NS, "B"): ConceptSpec(NS, "B", "decimal"),
    (NS, "C"): ConceptSpec(NS, "C", "decimal"),
    (NS, "Note"): ConceptSpec(NS, "Note", "text"),
}
MAPPING = {
    "#tot": "{" + NS + "}Total",
    "#a": "{" + NS + "}A",
    "#b": "{" + NS + "}B",
    "#c": "{" + NS + "}C",
    "#note": "{" + NS + "}Note",
}


def write(tmp: str, name: str, body: str) -> str:
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def link(inner: str, role: str = "http://example.com/role/r") -> str:
    return (
        '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<link:calculationLink xlink:type="extended" xlink:role="{role}">{inner}'
        "</link:calculationLink></link:linkbase>"
    )


def arc(frm: str, to: str, weight: str = "1", **attrs) -> str:
    extras = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return (
        f'<link:calculationArc xlink:type="arc" xlink:from="{frm}" xlink:to="{to}" '
        f'xlink:arcrole="{ARCROLE}" weight="{weight}"{extras}/>'
    )


class LinkbaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="calc_link_test_")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def resolve(self, body: str, mapping=None, concepts=None):
        path = write(self.tmp, "lb.xml", body)
        parsed = parse_linkbase_file(path)
        return resolve_networks(
            parsed, mapping or MAPPING, concepts or CONCEPTS
        )


class TestMapping(LinkbaseBase):
    def test_flat_and_nested_mapping_shapes_equivalent(self):
        flat = {"#a": "{" + NS + "}A"}
        nested = {"mappings": [{"fragment": "#a",
                                "concept": "{" + NS + "}A"}]}
        self.assertEqual(load_locator_mapping(flat), load_locator_mapping(nested))

    def test_duplicate_fragment_rejected(self):
        with self.assertRaises(LinkbaseError):
            load_locator_mapping([
                {"fragment": "#a", "concept": "{" + NS + "}A"},
                {"fragment": "#a", "concept": "{" + NS + "}B"},
            ])

    def test_fragment_must_be_hash_form(self):
        with self.assertRaises(LinkbaseError):
            load_locator_mapping({"a": "{" + NS + "}A"})

    def test_concept_must_be_expanded_qname(self):
        with self.assertRaises(LinkbaseError):
            load_locator_mapping({"#a": "NotAnExpandedName"})


class TestStructuralRefusals(LinkbaseBase):
    def _refuse(self, body: str, needle: str):
        path = write(self.tmp, "bad.xml", body)
        with self.assertRaises(LinkbaseError) as caught:
            parse_linkbase_file(path)
        self.assertIn(needle, str(caught.exception))
        self.assertRegex(str(caught.exception), r":\d+:\d+:")

    def test_unsupported_arcrole_refused(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>'
            '<link:calculationArc xlink:type="arc" xlink:from="t" xlink:to="x" '
            'xlink:arcrole="http://example.com/other" weight="1"/>')
        self._refuse(body, "summation-item")

    def test_remote_href_refused(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="http://x/y.xml#tot" '
            'xlink:label="t"/>')
        self._refuse(body, "same-document")

    def test_duplicate_label_in_one_link_refused(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="dup"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="dup"/>')
        self._refuse(body, "duplicate xlink:label")

    def test_zero_and_non_finite_weight_refused(self):
        base = ('<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
                '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>')
        self._refuse(link(base + arc("t", "x", weight="0")), "non-zero")
        self._refuse(link(base + arc("t", "x", weight="1E3")), "exact decimal")

    def test_negative_priority_and_bad_use_refused(self):
        base = ('<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
                '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>')
        self._refuse(link(base + arc("t", "x", priority="-1")), "non-negative")
        self._refuse(link(base + arc("t", "x", use="banana")), "optional")

    def test_unknown_element_refused(self):
        body = (
            '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
            'xmlns:xlink="http://www.w3.org/1999/xlink"><link:footnoteLink/></link:linkbase>'
        )
        self._refuse(body, "only link:calculationLink")

    def test_malformed_xml_refused_with_location(self):
        self._refuse("<link:linkbase>", "not well-formed")


class TestNetworkResolution(LinkbaseBase):
    def test_labels_are_scoped_per_link_same_text_different_concepts(self):
        body = (
            '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<link:calculationLink xlink:type="extended" xlink:role="r1">'
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="shared"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>'
            + arc("shared", "x") +
            "</link:calculationLink>"
            '<link:calculationLink xlink:type="extended" xlink:role="r2">'
            '<link:loc xlink:type="locator" xlink:href="#b" xlink:label="shared"/>'
            '<link:loc xlink:type="locator" xlink:href="#c" xlink:label="y"/>'
            + arc("shared", "y") +
            "</link:calculationLink></link:linkbase>"
        )
        networks = {n.role: n for n in self.resolve(body)}
        edge1 = networks["r1"].edges[0]
        edge2 = networks["r2"].edges[0]
        self.assertEqual(edge1.total, "{" + NS + "}Total")
        self.assertEqual(edge1.item, "{" + NS + "}A")
        self.assertEqual(edge2.total, "{" + NS + "}B")
        self.assertEqual(edge2.item, "{" + NS + "}C")

    def test_roles_are_isolated(self):
        body = (
            '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<link:calculationLink xlink:type="extended" xlink:role="r1">'
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>'
            + arc("t", "x") + "</link:calculationLink>"
            '<link:calculationLink xlink:type="extended" xlink:role="r2">'
            '<link:loc xlink:type="locator" xlink:href="#b" xlink:label="u"/>'
            '<link:loc xlink:type="locator" xlink:href="#c" xlink:label="v"/>'
            + arc("u", "v") + "</link:calculationLink></link:linkbase>"
        )
        networks = self.resolve(body)
        self.assertEqual(len(networks), 2)
        self.assertEqual({n.role for n in networks}, {"r1", "r2"})

    def test_highest_priority_overrides_lower_weight(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>'
            + arc("t", "x", weight="3", priority="0")
            + arc("t", "x", weight="1", priority="5"))
        network = self.resolve(body)[0]
        self.assertEqual(len(network.edges), 1)
        self.assertEqual(network.edges[0].weight, Decimal("1"))
        dispositions = {t.disposition for t in network.traces}
        self.assertEqual(dispositions, {"overridden", "adopted"})

    def test_highest_layer_prohibition_cancels_relation(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>'
            + arc("t", "x", weight="1", priority="2", use="optional")
            + arc("t", "x", weight="1", priority="4", use="prohibited"))
        network = self.resolve(body)[0]
        self.assertEqual(network.edges, ())
        self.assertTrue(all(t.disposition in ("overridden", "prohibited")
                            for t in network.traces))

    def test_duplicate_equal_edges_merge_to_one_with_all_locations(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>'
            + arc("t", "x", weight="1") + arc("t", "x", weight="1"))
        network = self.resolve(body)[0]
        self.assertEqual(len(network.edges), 1)
        self.assertEqual(len(network.edges[0].locations), 2)
        self.assertIn("merged", [t.disposition for t in network.traces])

    def test_top_layer_weight_clash_refuses(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>'
            + arc("t", "x", weight="1", priority="3")
            + arc("t", "x", weight="2", priority="3"))
        with self.assertRaises(LinkbaseError) as caught:
            self.resolve(body)
        self.assertIn("conflicting weights", str(caught.exception))

    def test_dangling_endpoint_refused(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            + arc("t", "ghost"))
        with self.assertRaises(LinkbaseError) as caught:
            self.resolve(body)
        self.assertIn("no matching link:loc", str(caught.exception))

    def test_unmapped_fragment_refused(self):
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="x"/>'
            + arc("t", "x"))
        with self.assertRaises(LinkbaseError) as caught:
            self.resolve(body, mapping={"#tot": "{" + NS + "}Total"})
        self.assertIn("not present in the explicit locator mapping",
                      str(caught.exception))

    def test_non_decimal_concept_refused(self):
        mapping = dict(MAPPING)
        mapping["#a"] = "{" + NS + "}Note"
        body = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="t"/>'
            '<link:loc xlink:type="locator" xlink:href="#note" xlink:label="x"/>'
            + arc("t", "x"))
        with self.assertRaises(LinkbaseError) as caught:
            self.resolve(body, mapping=mapping)
        self.assertIn("non-decimal", str(caught.exception))

    def test_long_cycle_named_in_error_and_self_loop_rejected(self):
        cycle_body = (
            '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<link:calculationLink xlink:type="extended" xlink:role="r">'
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="a"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="b"/>'
            '<link:loc xlink:type="locator" xlink:href="#b" xlink:label="c"/>'
            + arc("a", "b") + arc("b", "c") + arc("c", "a")
            + "</link:calculationLink></link:linkbase>"
        )
        with self.assertRaises(LinkbaseError) as caught:
            self.resolve(cycle_body)
        self.assertIn("contains a cycle", str(caught.exception))

        self_loop = link(
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="a"/>'
            + arc("a", "a"))
        with self.assertRaises(LinkbaseError) as caught:
            self.resolve(self_loop)
        self.assertIn("self-loop", str(caught.exception))

    def test_prohibited_edge_breaks_otherwise_cyclic_graph(self):
        body = (
            '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<link:calculationLink xlink:type="extended" xlink:role="r">'
            '<link:loc xlink:type="locator" xlink:href="#tot" xlink:label="a"/>'
            '<link:loc xlink:type="locator" xlink:href="#a" xlink:label="b"/>'
            '<link:loc xlink:type="locator" xlink:href="#b" xlink:label="c"/>'
            + arc("a", "b") + arc("b", "c")
            + arc("c", "a", use="prohibited")
            + "</link:calculationLink></link:linkbase>"
        )
        network = self.resolve(body)[0]
        self.assertEqual(len(network.edges), 2)


if __name__ == "__main__":
    unittest.main()
