"""Shared fixtures for controlled_query tests.

Builds a small hand-written warehouse with two enterprise entities that
share one identifier under different schemes, a one-hop relation (facts via
contexts) and a two-hop composite-key relation (shipments via lanes via
operators), plus rows exercising NULL keys, unmatched keys, BLOBs and big
integers.  The same shapes are used by the demo with a real XBRL-ingested
warehouse.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

SCHEME_LEI = "http://example.com/lei"
SCHEME_NAT = "http://example.com/national"
E1 = (SCHEME_LEI, "ACME-7")      # entity "甲"
E2 = (SCHEME_NAT, "ACME-7")      # entity "乙": same identifier, other scheme
E3 = (SCHEME_LEI, "OTHER-9")     # a third entity

WAREHOUSE_DDL = """
CREATE TABLE contexts (
    context_digest TEXT PRIMARY KEY,
    entity_scheme TEXT NOT NULL,
    entity_identifier TEXT NOT NULL
);
CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    concept TEXT NOT NULL,
    context_digest TEXT NOT NULL,
    value_text TEXT,
    decimals TEXT,
    language TEXT
);
CREATE TABLE operators (
    op_code TEXT,
    scheme TEXT,
    identifier TEXT,
    marker TEXT
);
CREATE TABLE lanes (
    region TEXT,
    hub TEXT,
    op_code TEXT
);
CREATE TABLE shipments (
    ship_id INTEGER PRIMARY KEY,
    lane_region TEXT,
    lane_hub TEXT,
    amount INTEGER,
    note TEXT,
    payload BLOB,
    marker TEXT
);
"""

# facts: (id, concept, context, value_text, decimals, language)
FACT_ROWS = [
    (1, "rev", "cA", "100", "2", "en"),
    (2, "rev", "cB", "200", "2", None),
    (3, "nil", "cA", None, "2", None),
    (4, "rev", "cC", "300", "2", "fr"),
]
CONTEXT_ROWS = [
    ("cA", E1[0], E1[1]),
    ("cB", E2[0], E2[1]),
    ("cC", E3[0], E3[1]),
]
OPERATOR_ROWS = [
    ("OP1", E1[0], E1[1], "m-old"),
    ("OP2", E2[0], E2[1], "m-old"),
]
LANE_ROWS = [
    ("EU", "H1", "OP1"),
    ("EU", "H2", "OP2"),
]
# shipments: (ship_id, lane_region, lane_hub, amount, note, payload, marker)
SHIPMENT_ROWS = [
    (1, "EU", "H1", 100, "ok", b"\x00\xff", "m-old"),
    (2, "EU", "H2", 200, "ok", b"\x01", "m-old"),
    (3, None, "H9", 300, "null-key", None, "m-old"),      # NULL left key
    (4, "XX", "H9", 400, "unmatched", None, "m-old"),     # no such lane
    (5, "EU", "H1", 9007199254740993, "big", b"", "m-old"),
]


def build_warehouse(path: str, *, wal: bool = False) -> None:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(WAREHOUSE_DDL)
    conn.executemany("INSERT INTO contexts VALUES (?,?,?)", CONTEXT_ROWS)
    conn.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?)", FACT_ROWS)
    conn.executemany("INSERT INTO operators VALUES (?,?,?,?)", OPERATOR_ROWS)
    conn.executemany("INSERT INTO lanes VALUES (?,?,?)", LANE_ROWS)
    conn.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?)", SHIPMENT_ROWS)
    conn.commit()
    conn.close()


def catalog_document() -> dict:
    return {
        "version": 1,
        "relations": [
            {
                "name": "fact_lines",
                "source": "facts",
                "fields": [
                    {"name": "fact id", "column": "id"},
                    {"name": "concept"},
                    {"name": "raw value", "column": "value_text"},
                    {"name": "marker", "column": "language"},
                ],
                "owner": {
                    "path": [
                        {"table": "contexts",
                         "left": ["context_digest"],
                         "right": ["context_digest"]}
                    ],
                    "entity": {"scheme": "entity_scheme",
                               "identifier": "entity_identifier"},
                },
                "summary_hidden": ["raw value"],
            },
            {
                "name": "shipment_lines",
                "source": "shipments",
                "fields": [
                    {"name": "ship id", "column": "ship_id"},
                    {"name": "amount"},
                    {"name": "note"},
                    {"name": "payload"},
                    {"name": "marker"},
                ],
                "owner": {
                    "path": [
                        {"table": "lanes",
                         "left": ["lane_region", "lane_hub"],
                         "right": ["region", "hub"]},
                        {"table": "operators",
                         "left": ["op_code"],
                         "right": ["op_code"]},
                    ],
                    "entity": {"scheme": "scheme", "identifier": "identifier"},
                },
                "summary_hidden": ["amount", "payload"],
            },
        ],
    }


def policy_document() -> dict:
    return {
        "version": 1,
        "identities": [
            {"name": "alice", "groups": []},                    # full E1
            {"name": "betty", "groups": []},                    # full E2
            {"name": "sally", "groups": []},                    # summary E2
            {"name": "ginny", "groups": ["g-full", "g-sum"]},   # group downgrade
            {"name": "danny", "groups": ["g-deny"]},            # deny beats allow
            {"name": "nancy", "groups": []},                    # no grants at all
            {"name": "mixed", "groups": []},                    # full E1 + summary E2
        ],
        "grants": [
            {"subject": "alice", "scheme": E1[0], "identifier": E1[1],
             "effect": "full"},
            {"subject": "betty", "scheme": E2[0], "identifier": E2[1],
             "effect": "full"},
            {"subject": "sally", "scheme": E2[0], "identifier": E2[1],
             "effect": "summary"},
            {"subject": "g-full", "scheme": E1[0], "identifier": E1[1],
             "effect": "full"},
            {"subject": "g-sum", "scheme": E1[0], "identifier": E1[1],
             "effect": "summary"},
            {"subject": "danny", "scheme": E1[0], "identifier": E1[1],
             "effect": "full"},
            {"subject": "g-deny", "scheme": E1[0], "identifier": E1[1],
             "effect": "deny"},
            {"subject": "mixed", "scheme": E1[0], "identifier": E1[1],
             "effect": "full"},
            {"subject": "mixed", "scheme": E2[0], "identifier": E2[1],
             "effect": "summary"},
        ],
    }


def write_json(path: str, document) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
    return path


def write_catalog(path: str, document=None) -> str:
    return write_json(path, document if document is not None else catalog_document())


def write_policy(path: str, document=None) -> str:
    return write_json(path, document if document is not None else policy_document())


def display_rows(report) -> list:
    """Rows as tuples of display strings, for compact assertions."""
    return [tuple(value.display() for value in row) for row in report.rows]
