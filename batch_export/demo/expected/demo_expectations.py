# Hand-written expectations for the demo XBRL files.
#
# THE VALUES IN THIS FILE ARE WRITTEN BY HAND from the demo XML, and are never
# produced by calling the ingester's canonicalisation helpers. The tests
# compare the ingester against these literals instead.
#
# Prefixes / context IDs / unit IDs / dimension order / unrelated whitespace in
# the source must not appear in any expected value below.

CO = "http://example.com/co"
UNITS = "http://example.com/units"
LEGACY = "http://example.com/legacy-units"
LEI = "http://example.com/lei"
INTERNAL = "http://example.com/internal-id"

REVENUE = "{" + CO + "}Revenue"
TINY = "{" + CO + "}Tiny"
HUGE = "{" + CO + "}Huge"
NOTHING = "{" + CO + "}Nothing"
ZERO = "{" + CO + "}Zero"
NOTE = "{" + CO + "}Note"
ASSETS = "{" + CO + "}Assets"

USD = "{" + UNITS + "}USD"
SHARES = "{" + UNITS + "}shares"
LEGACY_USD = "{" + LEGACY + "}USD"

PRODUCT_LINE = "{" + CO + "}ProductLine"
REGION = "{" + CO + "}Region"
RETAIL = "{" + CO + "}Retail"
EMEA = "{" + CO + "}EMEA"

# (dimension, member) pairs, sorted by dimension name, as the coordinate must
# expose them. Demo-A writes ProductLine first; demo-B writes Region first.
DIMS_ALPHA = ((PRODUCT_LINE, RETAIL), (REGION, EMEA))

# demo-A.xml, by document-order fact position (1-based).
DEMO_A = {
    "path": "xbrl_fixtures/demo-A.xml",
    "fact_count": 8,
    "context_count": 2,
    "unit_count": 3,
    "facts": [
        # position, concept, value_text, nil, language, unit
        (1, REVENUE, "1234.50", False, None, ("USD",)),
        (2, TINY, "0.000000000001", False, None, ("USD", "shares")),
        (3, REVENUE, "1.0", False, None, ("USD",)),
        (4, REVENUE, "1.00", False, None, ("USD",)),
        (5, HUGE, "1234567890123456789012345678901234567890", False, None, ("USD",)),
        (6, NOTHING, None, True, None, ("USD",)),
        (7, ZERO, "0", False, None, ("USD",)),
        (8, NOTE, "  retail segment  ", False, "en", None),
    ],
}

# demo-B.xml, by document-order fact position.
DEMO_B = {
    "path": "xbrl_fixtures/demo-B.xml",
    "fact_count": 5,
    "context_count": 2,
    "unit_count": 3,
    "facts": [
        (1, REVENUE, "1250.00", False, None, ("USD",)),
        (2, REVENUE, "1.000", False, None, ("USD",)),
        (3, TINY, "0.000000000001", False, None, ("USD", "shares")),
        (4, ZERO, "0.00", False, None, ("USD",)),
        (5, NOTE, "retail segment", False, "en", None),
    ],
}

DEMO_C = {
    "path": "xbrl_fixtures/demo-C.xml",
    "fact_count": 2,
    "context_count": 2,
    "unit_count": 1,
}

DEMO_D = {
    "path": "xbrl_fixtures/demo-D.xml",
    "fact_count": 4,
    "context_count": 1,
    "unit_count": 0,
    "facts": [
        (1, NOTE, "alpha", False, "en", None),
        (2, NOTE, "alpha", False, "fr", None),
        (3, NOTE, "alpha", False, "de", None),
        (4, NOTE, "beta", False, "en", None),
    ],
}

# After ingesting A, B, C and D: coordinate -> (status, sorted candidate count,
# number of distinct source files contributing). These counts are counted from
# the XML by hand; every "conflict" listed really has different values.
EXPECTED_RESULTS = [
    # concept, period kind/instant, language, status, candidate count
    (REVENUE, "duration", DIMS_ALPHA, None, "conflict", 2),
    (REVENUE, "instant", (), None, "single", 3),
    (TINY, "instant", (), None, "single", 2),
    (HUGE, "instant", (), None, "single", 1),
    (NOTHING, "instant", (), None, "single", 1),
    (ZERO, "instant", (), None, "single", 1),
    # Zero from demo-B expands from the same coordinate as demo-A's Zero, i.e.
    # the two zeros must be one result with two candidates.
    (NOTE, "instant", (), "en", "conflict", 2),   # demo-A note vs demo-B note
    (NOTE, "instant", (), "fr", "single", 1),
    (NOTE, "instant", (), "de", "single", 1),
    (ASSETS, "instant", (), None, "single", 1),
]

# Malformed files. (name, expected error class name, text that must appear in
# the message) -- every one must leave fact/source counts unchanged.
REFUSED = [
    ("bad-missing-ref.xml", "MissingReferenceError", "c-missing"),
    ("bad-duplicate-dim.xml", "XbrlStructureError", "duplicate dimension"),
    ("bad-dup-context-id.xml", "DuplicateIdError", "duplicate context id"),
    ("bad-dup-unit-id.xml", "DuplicateIdError", "duplicate unit id"),
    ("bad-typed-member.xml", "XbrlStructureError", "typedMember"),
    ("bad-tuple.xml", "XbrlStructureError", "nested content"),
    ("bad-unknown-concept.xml", "UnknownConceptError", "UndefinedConcept"),
    ("bad-unbound-prefix.xml", "XbrlXmlError", "zz"),
    ("bad-bad-date.xml", "InvalidValueError", "2024-02-30"),
    ("bad-end-before-start.xml", "XbrlStructureError", "end date"),
    ("bad-missing-decimals.xml", "XbrlStructureError", "decimals"),
    ("bad-nil-with-value.xml", "InvalidValueError", "nil"),
    ("bad-text-with-unit.xml", "XbrlStructureError", "unitRef"),
    ("bad-no-root.xml", "XbrlStructureError", "root"),
]
