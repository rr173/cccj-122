# Hand-written expectations for the calc-check demonstration.
#
# Every number in this file was derived by hand on paper from the rule
#
#     decimals = d  ->  [ v - 0.5*10^(-d), v + 0.5*10^(-d) ]
#     decimals = INF -> point interval [v, v]
#
# Nothing here is produced by importing calc_check.intervals or
# calc_check.network; the tests compare production output against these
# independent literals.
#
# Bounds are written as plain decimal strings ("99.5", "-250", ...).

LEI_SCHEME = "http://example.com/lei"
LEI_ID = "LEI-900"
DURATION = ("2024-01-01", "2024-12-31")
USD = "{http://example.com/units}USD"
EUR = "{http://example.com/units}EUR"
CHANNEL = "{http://example.com/calc}Channel"
ONLINE = "{http://example.com/calc}Online"
RETAIL = "{http://example.com/calc}Retail"

# The seven networks the valid linkbase must resolve (role suffix -> totals).
ROLE_INTERVAL = "http://example.com/role/interval-cases"
ROLE_COVERAGE = "http://example.com/role/coverage-cases"
ROLE_COORDINATE = "http://example.com/role/coordinate-cases"
ROLE_TWO_LAYER = "http://example.com/role/two-layer"
ROLE_OVERRIDE = "http://example.com/role/priority-override"
ROLE_PROHIBITION = "http://example.com/role/prohibition"
ROLE_BROKEN_CYCLE = "http://example.com/role/broken-cycle"

# Effective edge counts per role (duplicate Dup edge merged; prohibition edge
# removed; broken-cycle prohibition removed leaving two edges).
EXPECTED_EDGE_COUNTS = {
    ROLE_INTERVAL: 17,
    ROLE_COVERAGE: 5,
    ROLE_COORDINATE: 2,
    ROLE_TWO_LAYER: 2,
    ROLE_OVERRIDE: 1,
    ROLE_PROHIBITION: 0,
    ROLE_BROKEN_CYCLE: 2,
}

# The priority-override network must end with weight 1, not the original 3.
OVERRIDE_TOTAL = "{http://example.com/calc}IsolatedTotal"
OVERRIDE_ITEM = "{http://example.com/calc}IsolatedItem"
OVERRIDE_WEIGHT = "1"
OVERRIDE_DISPOSITIONS = ("overridden", "adopted")

# The prohibition network has no effective edge but keeps two located traces:
# the lower-priority optional edge is overridden by the higher-priority
# prohibited arc, which itself is recorded as prohibited.
PROHIBITION_TRACE_DISPOSITIONS = ("overridden", "prohibited")

# The broken-cycle network keeps an explicit prohibited trace and two edges.
BROKEN_CYCLE_DISPOSITIONS = ("adopted", "adopted", "prohibited")

# ---------------------------------------------------------------------------
# S1 + S2 verdicts, each derived by hand.
# Keys are total concept local names; coordinates are identified in prose.
# Each entry: verdict, total interval, weighted-sum interval (None when
# indeterminate), and the weighted item bounds in edge order.
# ---------------------------------------------------------------------------

S12_EXPECTATIONS = {
    # 100 d=0 -> [99.5, 100.5]. Items (after intersecting same-value S1/S2
    # candidates): 60.3 d=1 -> [60.25,60.35], 40.2 d=1 -> [40.15,40.25];
    # sum [100.40, 100.60]; overlap (100.40..100.50) -> consistent even though
    # displayed 100 != 60.3 + 40.2 = 100.5.
    "OverlapTotal": ("consistent", ("99.5", "100.5"), ("100.40", "100.60")),
    # INF points: 600 + 401 = 1001 != total 1000 -> inconsistent.
    "ExactTotal": ("inconsistent", ("1000", "1000"), ("1001", "1001")),
    # Touch: 50.5 d=1 [50.45,50.55] + 49.9 d=1 [49.85,49.95] = [100.30,100.50];
    # upper 100.50 touches total lower 99.5's upper edge -> consistent.
    "TouchTotal": ("consistent", ("99.5", "100.5"), ("100.30", "100.50")),
    # Gap: 50.4 d=1 [50.35,50.45] + 48.9 d=1 [48.85,48.95] = [99.20,99.40];
    # 99.40 < 99.5 -> disjoint -> inconsistent.
    "GapTotal": ("inconsistent", ("99.5", "100.5"), ("99.20", "99.40")),
    # Negative weight, decimals=-2 -> half width 50. Gross [1450,1550],
    # return [150,250] scaled by -1 -> [-250,-150]; sum [1200,1400]; total
    # 1300 d=-2 -> [1250,1350]; overlap -> consistent.
    "SignedTotal": ("consistent", ("1250", "1350"), ("1200", "1400")),
    # Coarse d=-3 half width 500: total 10000 -> [9500,10500]; items
    # 6000 -> [5500,6500], 4500 -> [4000,5000]; sum [9500,11500]; intersects ->
    # consistent despite displayed gap of 500.
    "CoarseTotal": ("consistent", ("9500", "10500"), ("9500", "11500")),
    # 40-digit INF points: A + B equals the total digit for digit -> consistent.
    "HugeTotal": (
        "consistent",
        ("1234567890123456789012345678901234567890",
         "1234567890123456789012345678901234567890"),
        ("1234567890123456789012345678901234567890",
         "1234567890123456789012345678901234567890"),
    ),
    # Same A+B, total one larger at the 40th digit -> point intervals disjoint.
    "HugeDisjointTotal": (
        "inconsistent",
        ("1234567890123456789012345678901234567891",
         "1234567890123456789012345678901234567891"),
        ("1234567890123456789012345678901234567890",
         "1234567890123456789012345678901234567890"),
    ),
    # Duplicate equal facts, two candidates on each side. Total 55 with
    # decimals d=2 (half 0.005) and d=1 (half 0.05): the intersection is the
    # tighter interval [54.995, 55.005]. Item 55 with d=2 (half 0.005) and d=3
    # (half 0.0005): intersection [54.9995, 55.0005]. One weighted edge even
    # though two candidate facts are retained.
    "DupTotal": ("consistent", ("54.995", "55.005"), ("54.9995", "55.0005")),
    # Real zero: 0 d=2 -> [-0.005, 0.005] both sides -> consistent.
    "ZeroTotal": ("consistent", ("-0.005", "0.005"), ("-0.005", "0.005")),
    # nil total -> indeterminate.
    "NilTotal": ("indeterminate", None, None),
    # total never reported -> indeterminate (items 30 and 70 present).
    "MissingTotal": ("indeterminate", None, None),
    # S1 only: single equal 40 facts -> consistent before S3 clashes them.
    "ConflictTotal": ("consistent", ("39.995", "40.005"), ("39.995", "40.005")),
    # DimTotal at the Online coordinate matches its Online item -> consistent.
    # The other two DimTotal coordinates (Retail total without item; no-dim
    # item without total) are indeterminate.
    "DimTotal_consistent_coordinate_dims": ((CHANNEL, ONLINE),),
    # No-dimension coordinate (item only, no total) and the Retail coordinate
    # (total reported, item missing), each keyed as a tuple of [name, member].
    "DimTotal_indeterminate_coordinate_dims": ((), ((CHANNEL, RETAIL),)),
    # EUR coordinate consistent; the USD item coordinate has no USD total.
    "EurTotal_consistent_unit": EUR,
    # Two layers: LayerMid -> LayerLeaf equal 60 -> consistent; LayerTop ->
    # LayerMid: 100 vs 60 -> inconsistent. The leaf is never promoted to fill
    # LayerTop, proving no fabricated intermediate.
    "LayerMid": ("consistent", ("59.995", "60.005"), ("59.995", "60.005")),
    "LayerTop": ("inconsistent", ("99.995", "100.005"), ("59.995", "60.005")),
    # Priority override made the effective weight 1: 70 = 70 -> consistent.
    "IsolatedTotal": ("consistent", ("69.995", "70.005"), ("69.995", "70.005")),
    # Broken cycle chain: CycleA -> CycleB (30 = 30) consistent; CycleB ->
    # CycleC (30 vs 12) inconsistent. The prohibited C->A edge never counts.
    "CycleA": ("consistent", ("29.995", "30.005"), ("29.995", "30.005")),
    "CycleB": ("inconsistent", ("29.995", "30.005"), ("11.995", "12.005")),
}

# S1-only counts (ConflictTotal still consistent; S2 duplicates absent but the
# same-valued S1 facts remain, so the deterministic totals do not change).
S1_COUNTS = {"consistent": 12, "inconsistent": 5, "indeterminate": 5}
S12_COUNTS = {"consistent": 13, "inconsistent": 5, "indeterminate": 5}
# Adding S3 turns ConflictTotal and SignedTotal indeterminate and adds a
# nil-vs-value clash at NilItem; NilTotal stays indeterminate for two reasons.
ALL_COUNTS = {"consistent": 11, "inconsistent": 5, "indeterminate": 7}

# Totals that must be indeterminate specifically because of S3 conflicts.
S3_CONFLICT_TOTALS = ("ConflictTotal", "SignedTotal")

# The coverage network has no "no facts at all" total in this fixture, but a
# separate empty-network expectation is exercised in the unit tests with a
# purpose-built database; here MissingTotal is indeterminate, not unchecked.

# Candidate source/position traces for the duplicate relationship (S2 only).
# Local fact positions are 1-based within calc-source-2.xml.
DUP_CANDIDATE_POSITIONS = {
    "DupTotal": (4, 5),
    "DupItem": (6, 7),
}
