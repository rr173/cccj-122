#!/usr/bin/env bash
# Terminal walkthrough of the controlled-query CLI: builds a warehouse in a
# temp directory, runs queries as different identities, and shows the exit
# codes for a rejection and for check-config.
set -u
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT"
export PYTHONPATH="$PROJECT"
WORK="$(mktemp -d)"
export WORK
trap 'rm -rf "$WORK"' EXIT

echo "== build the demo warehouse (via the demo script's stage 1) =="
python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join("demo", "controlled_query_demo"))
sys.path.insert(0, ".")
import run_controlled_query_demo as demo
work = os.environ["WORK"]
demo.build_warehouse(os.path.join(work, "warehouse.sqlite"))
demo.write_json(os.path.join(work, "catalog.json"), demo.catalog_document())
demo.write_json(os.path.join(work, "policy.json"), demo.policy_document())
print("warehouse + catalog + policy written")
PY

DB="$WORK/warehouse.sqlite"
CAT="$WORK/catalog.json"
POL="$WORK/policy.json"

echo
echo "== check-config =="
python3 -m controlled_query.cli check-config "$DB" --catalog "$CAT" --policy "$POL"
echo "exit=$?"

echo
echo "== same query, three identities =="
for ID in ava sara nina; do
  echo "-- $ID --"
  python3 -m controlled_query.cli run "$DB" --catalog "$CAT" --policy "$POL" \
    --identity "$ID" \
    --sql 'SELECT COUNT(*) AS visible, COUNT("raw value") AS valued FROM fact_lines' \
    --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rows:", d["rows"])'
done

echo
echo "== a rejection (physical table) =="
python3 -m controlled_query.cli run "$DB" --catalog "$CAT" --policy "$POL" \
  --identity ava --sql 'SELECT * FROM facts'
echo "exit=$?"

echo
echo "== result file =="
python3 -m controlled_query.cli run "$DB" --catalog "$CAT" --policy "$POL" \
  --identity sara --sql 'SELECT "fact id", concept FROM fact_lines ORDER BY "fact id"' \
  --result "$WORK/result.json"
echo "exit=$?"
cat "$WORK/result.json"
