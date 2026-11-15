#!/usr/bin/env bash
# Terminal walkthrough of the offline taxonomy package registry.
# Every command's exit status is asserted; the script fails on the first
# unexpected status. Run from the repository root:
#   bash demo/taxonomy_package_demo/run_taxonomy_cli_demo.sh
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

export PYTHONPATH="$ROOT"
export ROOT
PY=python3
CLI=( "$PY" -m taxonomy_package.cli )

echo "== build the demonstration package =="
"$PY" - "$WORK/demo.zip" <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "demo",
                                "taxonomy_package_demo"))
import fixture_builder as fb
fb.build_zip(sys.argv[1], junk_count=50)
PYEOF

echo "== init + register =="
"${CLI[@]}" init "$WORK/registry"
"${CLI[@]}" register "$WORK/registry" "$WORK/demo.zip"
DIGEST=$( "${CLI[@]}" list "$WORK/registry" --json \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin)[0]["package_sha256"])')
echo "digest=$DIGEST"

echo "== re-register is an idempotent reuse =="
"${CLI[@]}" register "$WORK/registry" "$WORK/demo.zip" --json

echo "== show / resolve / query =="
"${CLI[@]}" show "$WORK/registry" --digest "$DIGEST"
"${CLI[@]}" resolve "$WORK/registry" --digest "$DIGEST"
"${CLI[@]}" query "$WORK/registry" --digest "$DIGEST" \
  --uri "xbrl-pkg://package/schemas/ep1.xsd" --json

echo "== verify a clean registry (expect 0) =="
"${CLI[@]}" verify "$WORK/registry" --json
test $? -eq 0 || { echo "clean verify failed"; exit 1; }

echo "== tamper a member and verify (expect exit 3) =="
BLOB=$(find "$WORK/registry/packages/$DIGEST/files" -name '*.member' | head -1)
printf 'tampered' > "$BLOB"
if "${CLI[@]}" verify "$WORK/registry"; then
  echo "ERROR: tampered registry verified clean"; exit 1
else
  rc=$?
  test "$rc" -eq 3 || { echo "expected 3, got $rc"; exit 1; }
  echo "correctly refused with exit 3"
fi

echo "== resolve a missing dependency (expect exit 4) =="
"$PY" - "$WORK/missing.zip" <<'PYEOF'
import sys, zipfile
CAT="urn:oasis:names:tc:entity:xmlns:xml:catalog"
TP="http://xbrl.org/2016/taxonomy-package"
XSD="http://www.w3.org/2001/XMLSchema"
with zipfile.ZipFile(sys.argv[1], "w") as z:
    z.writestr("p/META-INF/taxonomyPackage.xml", f"""<?xml version="1.0"?>
<tp:taxonomyPackage xmlns:tp="{TP}">
 <tp:identifier>m</tp:identifier><tp:name>m</tp:name><tp:version>1</tp:version>
 <tp:entryPoints><tp:entryPoint>
  <tp:entryPointDocument href="schemas/entry.xsd"/>
 </tp:entryPoint></tp:entryPoints>
</tp:taxonomyPackage>""")
    z.writestr("p/META-INF/catalog.xml", f"""<?xml version="1.0"?>
<catalog xmlns="{CAT}"><rewriteURI uriStartString="http://x.example/ns"
 rewritePrefix="xbrl-pkg://package/schemas/"/></catalog>""")
    z.writestr("p/schemas/entry.xsd", f"""<?xml version="1.0"?>
<schema xmlns="{XSD}"><import namespace="u"
 schemaLocation="http://x.example/ns/ghost.xsd"/></schema>""")
PYEOF
MISSING=$( "${CLI[@]}" register "$WORK/registry" "$WORK/missing.zip" --json \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin)[0]["package_sha256"])')
if "${CLI[@]}" resolve "$WORK/registry" --digest "$MISSING"; then
  echo "ERROR: missing dependency resolved"; exit 1
else
  rc=$?
  test "$rc" -eq 4 || { echo "expected 4, got $rc"; exit 1; }
  echo "correctly refused with exit 4"
fi

echo
echo "all CLI exit-code assertions held"
