#!/usr/bin/env bash
# Terminal-command walkthrough against a throwaway warehouse.
# Demonstrates ingest -> query -> search -> trace -> describe -> show and the
# refusal exit code. Run from anywhere; exits non-zero if any expectation fails.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -r "$ROOT/demo/xbrl_fixtures" "$WORK/fixtures"
DB="$WORK/warehouse.sqlite"
DICT="$WORK/fixtures/concepts.json"
CLI=(python3 -m xbrl_ingest.cli)

cd "$ROOT"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "## ingest (one refusal is expected and must not write rows)"
set +e
"${CLI[@]}" ingest "$DB" --dict "$DICT" \
    "$WORK/fixtures/demo-A.xml" "$WORK/fixtures/demo-B.xml" \
    "$WORK/fixtures/bad-duplicate-dim.xml"
status=$?
set -e
[ "$status" -eq 2 ] || fail "refusal exit code was $status, expected 2"

echo
echo "## query: the duration Revenue coordinate is a conflict with two candidates"
"${CLI[@]}" query "$DB" --dict "$DICT" \
    --concept-local Revenue --period-kind duration > "$WORK/query.txt"
grep -q "CONFLICT" "$WORK/query.txt" || fail "duration Revenue not marked conflict"
grep -q "candidates: 2" "$WORK/query.txt" || fail "duration Revenue candidates != 2"
grep -q "1234.50" "$WORK/query.txt" || fail "missing 1234.50"
grep -q "1250.00" "$WORK/query.txt" || fail "missing 1250.00"
grep -q "ProductLine" "$WORK/query.txt" || fail "dimensions not shown"
cat "$WORK/query.txt"

echo
echo "## query: instant Revenue is one coordinate with three agreeing candidates"
"${CLI[@]}" query "$DB" --dict "$DICT" \
    --concept-local Revenue --period-kind instant > "$WORK/instant.txt"
grep -q "SINGLE" "$WORK/instant.txt" || fail "instant Revenue not single"
grep -q "candidates: 3" "$WORK/instant.txt" || fail "instant candidates != 3"

echo
echo "## search: text search returns the whole coordinate, not one winner"
"${CLI[@]}" search "$DB" --dict "$DICT" --exact "retail segment" > "$WORK/search.txt"
grep -q "CONFLICT" "$WORK/search.txt" || fail "text coordinate not conflict"
grep -q "retail segment" "$WORK/search.txt" || fail "trimmed text missing"
cat "$WORK/search.txt"

echo
echo "## trace: original occurrence of the first fact"
SRC=$("${CLI[@]}" sources "$DB" --dict "$DICT" --json \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["source_id"])')
"${CLI[@]}" trace "$DB" --dict "$DICT" --source "$SRC" --position 1 --json \
    > "$WORK/trace.json"
python3 - "$WORK/trace.json" <<'PY' || fail "trace assertions failed"
import json, sys
payload = json.load(open(sys.argv[1]))
assert payload["concept"] == "{http://example.com/co}Revenue", payload
assert payload["value_text"] == "1234.50", payload
assert payload["context_id_in_source"] == "c-alpha", payload
assert payload["unit_id_in_source"] == "u-money", payload
assert payload["position"] == 1, payload
PY
cat "$WORK/trace.json"

echo
echo "## show: same coordinate by digest, all candidates"
DIGEST=$(python3 - "$WORK/trace.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["coordinate"]["digest"])
PY
)
"${CLI[@]}" show "$DB" --dict "$DICT" --digest "$DIGEST" > "$WORK/show.txt"
grep -q "candidate_count" "$WORK/show.txt" || fail "show did not list candidates"

echo
echo "## describe: source identity and its canonical declarations"
"${CLI[@]}" describe "$DB" --dict "$DICT" "$SRC" > "$WORK/describe.json"
python3 - "$WORK/describe.json" <<'PY' || fail "describe assertions failed"
import json, sys
payload = json.load(open(sys.argv[1]))
assert len(payload["paths"]) == 1, payload
concepts = {c["id_in_source"] for c in payload["contexts"]}
assert concepts == {"c-alpha", "c-beta"}, concepts
units = {u["id_in_source"] for u in payload["units"]}
assert units == {"u-money", "u-ratio", "u-product"}, units
PY

echo
echo "## sources: two accepted sources (the refused file added none)"
"${CLI[@]}" sources "$DB" --dict "$DICT" --json > "$WORK/sources.json"
python3 - "$WORK/sources.json" <<'PY' || fail "source count assertion failed"
import json, sys
payload = json.load(open(sys.argv[1]))
assert len(payload) == 2, payload
assert all(s["content_sha256"] for s in payload), payload
PY

echo
echo "ALL CLI DEMO CHECKS PASSED"
