#!/usr/bin/env bash
# Terminal-command walkthrough for read-only calculation checks.
#
# Demonstrates the three distinguishable outcomes and exit statuses:
#   definition failure (3), indeterminate vs inconsistent business results,
#   invocation error (2), and a clean run (0). Run from anywhere; non-zero on
#   any failed expectation. Everything uses throwaway files.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FX="$ROOT/demo/calc_fixtures"
DB="$WORK/warehouse.sqlite"
DICT="$FX/concepts.json"
MAP="$FX/locator_mapping.json"
LB="$FX/calculations.xml"

cd "$ROOT"
fail() { echo "FAIL: $*" >&2; exit 1; }

# Isolated interpreters need the trusted project path inserted explicitly.
export PY="$WORK/.launcher.py"
cat > "$PY" <<'PYEOF'
import sys
sys.path.insert(0, __import__("os").environ["CALC_PROJECT"])
from calc_check.cli import main
raise SystemExit(main())
PYEOF
export CALC_PROJECT="$ROOT"
CLI=(python3 -I "$PY")

echo "## ingest the three demo sources (existing entry point, unchanged)"
python3 -I -c "
import sys; sys.path.insert(0, '$ROOT')
from xbrl_ingest import XbrlIngestor
import json, os
ing = XbrlIngestor('$DB', '$DICT')
ids = [ing.ingest_file(os.path.join('$FX', f'calc-source-{n}.xml')).source_id
       for n in (1, 2, 3)]
ing.close()
json.dump(ids, open('$WORK/sources.json', 'w'))
"
mapfile -t IDS < <(python3 -c "import json; [print(x) for x in json.load(open('$WORK/sources.json'))]")

echo
echo "## networks: effective edges and per-arc dispositions (definition OK)"
"${CLI[@]}" networks "$LB" --mapping "$MAP" --dict "$DICT" > "$WORK/net.json"
python3 - "$WORK/net.json" <<'PY' || fail "network assertions"
import json, sys
payload = json.load(open(sys.argv[1]))
roles = {n["role"]: n for n in payload["networks"]}
# prohibition role must resolve to zero edges
prob = next(n for n in payload["networks"] if n["role"].endswith("prohibition"))
assert prob["effective_edges"] == [], prob
assert any(t["disposition"] == "prohibited" for t in prob["arc_traces"])
# priority override ends at weight 1
ov = next(n for n in payload["networks"] if n["role"].endswith("priority-override"))
assert ov["effective_edges"][0]["weight"] == "1", ov
print("network resolution assertions held")
PY

echo
echo "## check S1+S2: real inconsistencies -> exit 4"
set +e
"${CLI[@]}" check "$DB" "$LB" --mapping "$MAP" --dict "$DICT" \
    --source "${IDS[0]}" --source "${IDS[1]}" \
    --report "$WORK/s12.json" > "$WORK/s12.txt"
status=$?
set -e
[ "$status" -eq 4 ] || fail "expected exit 4 for inconsistent, got $status"
python3 - "$WORK/s12.json" <<'PY' || fail "report assertions"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["counts"]["inconsistent"] >= 1, r["counts"]
assert r["counts"]["consistent"] >= 1, r["counts"]
# every check carries full coordinate, edges and traceable candidates
for item in r["checks"]:
    assert item["role"] and item["total"] and "coordinate" in item
    assert item["edges"] and "unit" in item["coordinate"]
    for cand in item["total_candidates"]:
        assert cand["source_id"] and isinstance(cand["position"], int)
assert "precision_notice" in r and "closed-interval" in r["precision_notice"]
print("machine-readable report assertions held")
PY
grep -q "verdict counts" "$WORK/s12.txt" || fail "human summary missing"

echo
echo "## definition failures -> exit 3 with located messages"
for pair in "bad-weight-clash.xml:conflicting weights" \
            "bad-dangling.xml:no matching link:loc" \
            "bad-cycle.xml:contains a cycle" \
            "bad-arcrole.xml:summation-item" \
            "bad-href.xml:same-document" \
            "bad-duplicate-label.xml:duplicate xlink:label" \
            "bad-self-loop.xml:self-loop"; do
    file="${pair%%:*}"; needle="${pair##*:}"
    set +e
    "${CLI[@]}" networks "$FX/$file" --mapping "$MAP" --dict "$DICT" \
        > "$WORK/bad.out" 2> "$WORK/bad.err"
    code=$?
    set -e
    [ "$code" -eq 3 ] || fail "$file expected exit 3, got $code"
    grep -q "$needle" "$WORK/bad.err" || fail "$file missing diagnostic $needle"
done
echo "all seven definition failures refused with exit 3"

echo
echo "## invocation error: unknown source -> exit 2"
set +e
"${CLI[@]}" check "$DB" "$LB" --mapping "$MAP" --dict "$DICT" \
    --source src_nope > /dev/null 2> "$WORK/inv.err"
code=$?
set -e
[ "$code" -eq 2 ] || fail "unknown source expected exit 2, got $code"
grep -q "unknown source" "$WORK/inv.err" || fail "unknown source message missing"

echo
echo "## clean run: a consistent-only selection exits 0"
python3 - "$DB" "$LB" "$MAP" "$DICT" "$WORK" <<'PY' || fail "clean setup"
import json, os, sys, sqlite3
sys.path.insert(0, os.environ["CALC_PROJECT"])
from xbrl_ingest import XbrlIngestor
from calc_check import CalculationChecker
db, lb, mp, dc, work = sys.argv[1:6]
body = (
    '<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:c="http://example.com/calc">'
    '<context id="c"><entity><identifier scheme="s">E</identifier></entity>'
    '<period><instant>2024-12-31</instant></period></context>'
    '<unit id="u"><measure xmlns:m="http://example.com/units">m:USD</measure></unit>'
    '<c:IsolatedTotal contextRef="c" unitRef="u" decimals="INF">70</c:IsolatedTotal>'
    '<c:IsolatedItem contextRef="c" unitRef="u" decimals="INF">70</c:IsolatedItem>'
    "</xbrl>"
)
path = os.path.join(work, "clean.xml")
open(path, "w").write(body)
sid = XbrlIngestor(db, dc).ingest_file(path).source_id
json.dump([sid], open(os.path.join(work, "clean.json"), "w"))
PY
mapfile -t CLEAN < <(python3 -c "import json; [print(x) for x in json.load(open('$WORK/clean.json'))]")
set +e
"${CLI[@]}" check "$DB" "$LB" --mapping "$MAP" --dict "$DICT" \
    --source "${CLEAN[0]}" > "$WORK/clean.out" 2> "$WORK/clean.err"
code=$?
set -e
[ "$code" -eq 0 ] || fail "clean run expected 0, got $code: $(cat "$WORK/clean.err")"
grep -q "UNCHECKED" "$WORK/clean.out" || fail "unchecked totals must be listed"

echo
echo "## read-only guarantee: no wal/shm/journal side files"
side=$(find "$WORK" -name '*-wal' -o -name '*-shm' -o -name '*-journal' || true)
[ -z "$side" ] || fail "read-only check created side files: $side"

echo
echo "ALL CALC CLI DEMO CHECKS PASSED"
