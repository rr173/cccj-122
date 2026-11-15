"""Hand-written expectations for the controlled-query demo.

Nothing in this module is computed by the controlled_query package: every
row below was written by hand from the fixture data.  The demo compares the
service's answers against these rows (via the plain-SQLite oracle), so the
official policy resolution and staging code is never used to produce its
own expectations.

Entities (scheme, identifier):

* E1 = (http://example.com/lei,      ACME-7)   -- "甲"
* E2 = (http://example.com/national, ACME-7)   -- "乙" (same identifier!)
* E3 = (http://example.com/lei,      OTHER-9)  -- nobody may see it

Warehouse content (built by the real XBRL ingestor + plain DDL):

facts (via contexts): 1 Revenue 1000.50 / 2 Assets 5000 / 3 Note 'lei note'
(lang en) belong to E1; 4 Revenue 2000.75 / 5 Assets 7000 belong to E2;
6 Revenue 999 belongs to E3.

shipments -> lanes (composite region+hub) -> operators:
* ship 1 (EU,H1, 100, 'fragile', X'00FF01') -> OP-LEI -> E1
* ship 2 (EU,H2, 200, 'heavy',   X'02')    -> OP-NAT -> E2
* ship 3 (NULL lane key)                   -> no owner, invisible to all
* ship 4 (lane AP/H9 not in lanes)         -> no owner, invisible to all
* ship 5 (EU,H1, 9007199254740993, 'big', X'') -> OP-LEI -> E1
* ship 6 (EU,H1, 50, 'linked', X'06', marker 'en') -> OP-LEI -> E1; its
  marker equals the Note fact's language, so the demo's LEFT JOIN has one
  real match plus preserved non-matching left rows
"""

SCHEME_LEI = "http://example.com/lei"
SCHEME_NAT = "http://example.com/national"
E1 = (SCHEME_LEI, "ACME-7")
E2 = (SCHEME_NAT, "ACME-7")
E3 = (SCHEME_LEI, "OTHER-9")

REVENUE = "{http://example.com/co}Revenue"
ASSETS = "{http://example.com/co}Assets"
NOTE = "{http://example.com/co}Note"

# Published shapes: (public field name, declared type) per relation, in
# published order.  The declared types are the source columns' types; the
# oracle builds its small database with exactly these.
RELATION_SHAPES = {
    "fact_lines": [
        ("fact id", "INTEGER"),
        ("concept", "TEXT"),
        ("raw value", "TEXT"),
        ("marker", "TEXT"),
    ],
    "shipment_lines": [
        ("ship id", "INTEGER"),
        ("amount", "INTEGER"),
        ("note", "TEXT"),
        ("payload", "BLOB"),
        ("marker", "TEXT"),
    ],
}

# The authorized rows each identity should see, per relation.  None is SQL
# NULL.  Under summary the hidden fields ("raw value"; amount, payload) are
# NULL -- they were replaced before any user computation.
EXPECTED = {
    "ava": {   # full on E1
        "fact_lines": [
            (1, REVENUE, "1000.50", None),
            (2, ASSETS, "5000", None),
            (3, NOTE, "lei note", "en"),
        ],
        "shipment_lines": [
            (1, 100, "fragile", b"\x00\xff\x01", "m1"),
            (5, 9007199254740993, "big", b"", "m1"),
            (6, 50, "linked", b"\x06", "en"),
        ],
    },
    "noah": {  # full on E2
        "fact_lines": [
            (4, REVENUE, "2000.75", None),
            (5, ASSETS, "7000", None),
        ],
        "shipment_lines": [
            (2, 200, "heavy", b"\x02", "m1"),
        ],
    },
    "sara": {  # summary on E2
        "fact_lines": [
            (4, REVENUE, None, None),
            (5, ASSETS, None, None),
        ],
        "shipment_lines": [
            (2, None, "heavy", None, "m1"),
        ],
    },
    "gwen": {  # groups g-full + g-sum on E1 -> summary (downgrade)
        "fact_lines": [
            (1, REVENUE, None, None),
            (2, ASSETS, None, None),
            (3, NOTE, None, "en"),
        ],
        "shipment_lines": [
            (1, None, "fragile", None, "m1"),
            (5, None, "big", None, "m1"),
            (6, None, "linked", None, "en"),
        ],
    },
    "dana": {  # own full grant on E1, group deny on E1 -> nothing
        "fact_lines": [],
        "shipment_lines": [],
    },
    "nina": {  # registered, no grants at all -> empty relations
        "fact_lines": [],
        "shipment_lines": [],
    },
    "mona": {  # full on E1 + summary on E2
        "fact_lines": [
            (1, REVENUE, "1000.50", None),
            (2, ASSETS, "5000", None),
            (3, NOTE, "lei note", "en"),
            (4, REVENUE, None, None),
            (5, ASSETS, None, None),
        ],
        "shipment_lines": [
            (1, 100, "fragile", b"\x00\xff\x01", "m1"),
            (5, 9007199254740993, "big", b"", "m1"),
            (6, 50, "linked", b"\x06", "en"),
            (2, None, "heavy", None, "m1"),
        ],
    },
}

# The demo queries.  Each is run by the service and by the oracle for the
# listed identities; results must agree exactly (columns, typed values,
# and order where the query has ORDER BY).
QUERIES = {
    # A field predicate: the hidden value 1000.50 exists in the source but
    # is NULL for gwen, so her predicate matches nothing.
    "field_predicate": (
        'SELECT "fact id" FROM fact_lines WHERE "raw value" = \'1000.50\''
        ' ORDER BY "fact id"',
        ["ava", "gwen", "mona"],
        True,
    ),
    # COUNT(*) counts visible rows; COUNT(hidden) skips the masked NULLs.
    "count_star_vs_count_hidden": (
        'SELECT COUNT(*) AS visible, COUNT("raw value") AS valued'
        " FROM fact_lines",
        ["ava", "gwen", "sara", "nina"],
        True,
    ),
    # LEFT JOIN keeps left rows; masked values stay NULL on both sides.
    # The Note fact (marker 'en') matches ship 6; the NULL-markered facts
    # are preserved with NULL right sides.
    "left_join_keeps_rows": (
        'SELECT f."fact id" AS id, f."raw value" AS rv, s.amount AS amt'
        " FROM fact_lines f LEFT JOIN shipment_lines s"
        "   ON s.marker = f.marker"
        " ORDER BY id",
        ["ava", "mona"],
        True,
    ),
    # Correlated EXISTS across the two public relations.
    "correlated_exists": (
        'SELECT "ship id" FROM shipment_lines s'
        " WHERE EXISTS (SELECT 1 FROM fact_lines f"
        "              WHERE f.marker = s.marker AND f.concept LIKE '%Note')"
        ' ORDER BY "ship id"',
        ["ava", "noah"],
        True,
    ),
    # GROUP BY / HAVING over masked and unmasked values.
    "group_having": (
        "SELECT marker AS m, COUNT(*) AS n, COUNT(amount) AS valued,"
        " SUM(amount) AS total, MIN(amount) AS lo, MAX(amount) AS hi"
        " FROM shipment_lines GROUP BY marker HAVING COUNT(*) >= 1"
        " ORDER BY m",
        ["ava", "gwen", "mona"],
        True,
    ),
    # UNION ALL + ORDER BY + LIMIT.
    "union_all_limit": (
        "SELECT concept AS label FROM fact_lines"
        " UNION ALL SELECT note FROM shipment_lines"
        " ORDER BY label LIMIT 4 OFFSET 1",
        ["mona"],
        True,
    ),
    # A CTE named like the physical table `facts`: legal, and it reads the
    # public relation only.
    "cte_shadows_physical_name": (
        "WITH facts AS (SELECT concept FROM fact_lines"
        "              WHERE concept LIKE '%Assets')"
        " SELECT * FROM facts",
        ["ava", "nina"],
        True,
    ),
    # Scalar functions, CASE, CAST over authorized rows.
    "scalar_toolkit": (
        'SELECT "ship id", COALESCE(CAST(amount AS TEXT), \'hidden\') AS a,'
        " NULLIF(note, 'fragile') AS n2, ABS(amount) AS abs_a,"
        " LENGTH(note) AS ln, SUBSTR(note, 1, 2) AS pre,"
        " TYPEOF(payload) AS pt,"
        " CASE WHEN amount IS NULL THEN 'masked' ELSE 'clear' END AS state"
        ' FROM shipment_lines ORDER BY "ship id"',
        ["ava", "gwen"],
        True,
    ),
    # Affinity: an INTEGER-affinity column compared with a text literal.
    "affinity_text_vs_integer": (
        'SELECT "ship id", amount FROM shipment_lines WHERE amount = \'100\'',
        ["ava"],
        True,
    ),
    # Empty result: columns exist, no rows.
    "empty_result": (
        "SELECT concept FROM fact_lines WHERE 1 = 0",
        ["ava"],
        True,
    ),
}
