#!/usr/bin/env bash
# Full test suite: unit tests, Blender integration tests, and the .NET conformance gate.
#
# The three layers check genuinely different things:
#   unit         our contract math against itself (fast, no Blender)
#   integration  our conversion against Blender's glTF exporter, and the live protocol
#   conformance  our JSON against the engine's own C# reader/writer
#
# Only the third can catch contract drift, and only the second can catch a wrong axis
# convention -- the unit tests would happily pass with both broken.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

BLENDER="${BLENDER:-blender}"
PYTHON="${PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

failures=0
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
check() { if [ "$1" -ne 0 ]; then echo "FAILED: $2"; failures=$((failures + 1)); fi; }

step "Unit tests (contract math, no Blender)"
"$PYTHON" -m pytest tests/unit -q
check $? "unit tests"

if command -v "$BLENDER" >/dev/null 2>&1; then
  step "Integration: axis parity vs Blender's glTF exporter"
  "$BLENDER" --background --factory-startup --python tests/integration/test_axis_parity.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -20
  check "${PIPESTATUS[0]}" "axis parity"

  step "Integration: live preview against the mock runtime"
  "$BLENDER" --background --factory-startup --python tests/integration/test_live_preview.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -25
  check "${PIPESTATUS[0]}" "live preview"

  step "Integration: GLB texture externalization (KTX2 sidecars)"
  "$BLENDER" --background --factory-startup --python tests/integration/test_glb_textures.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -20
  check "${PIPESTATUS[0]}" "glb textures"

  step "Integration: KTX2 transcoder dialects"
  "$BLENDER" --background --factory-startup --python tests/integration/test_ktx_pipeline.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -20
  check "${PIPESTATUS[0]}" "ktx pipeline"

  step "Integration: play failure diagnostics"
  "$BLENDER" --background --factory-startup --python tests/integration/test_play_diagnostics.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -20
  check "${PIPESTATUS[0]}" "play diagnostics"

  step "Integration: authored components (schema-driven)"
  "$BLENDER" --background --factory-startup --python tests/integration/test_authored_components.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -30
  check "${PIPESTATUS[0]}" "authored components"

  step "Integration: config documents (file-backed authored components)"
  "$BLENDER" --background --factory-startup --python tests/integration/test_config_documents.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -30
  check "${PIPESTATUS[0]}" "config documents"

  step "Integration: full scene export"
  "$BLENDER" --background --factory-startup --python tests/integration/test_export_scene.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -30
  check "${PIPESTATUS[0]}" "scene export"
else
  echo "SKIPPED: Blender not found (set BLENDER=/path/to/blender) — integration tests not run."
fi

if command -v dotnet >/dev/null 2>&1; then
  step "Conformance: the vendored engine schema matches Paradise.Export"
  # The addon cannot load a C# assembly, so it ships a copy of the engine's authored-component
  # schema. Value-compare it against the constant in the real Paradise.Export: a mismatch means
  # the engine grew or changed a component and the copy must be regenerated (see
  # contract/authoring.py read_engine_schema).
  dotnet run --project tools/ParadiseBlenderBridge -- engine-schema \
    | "$PYTHON" -c '
import json, sys
printed = json.loads(sys.stdin.read())
vendored = json.load(open("paradise_blender/contract/engine_authoring_schema.json"))
if printed != vendored:
    print("DRIFT: engine_authoring_schema.json no longer matches Paradise.Export.AuthoringSchema")
    sys.exit(1)
print("OK — the vendored engine schema matches Paradise.Export.")
'
  check $? "vendored engine schema"

  step "Conformance: round-trip exported documents through Paradise.Export"
  # The export test above writes its documents here.
  DATA="${TMPDIR:-/tmp}/paradise_export_test"
  if [ -d "$DATA" ]; then
    for document in "$DATA"/scenes/*.json "$DATA"/materials/*.json "$DATA"/ProjectSettings.json; do
      [ -f "$document" ] || continue
      dotnet run --project tools/ParadiseBlenderBridge -- contract-check "$document"
      check $? "contract-check $(basename "$document")"
    done
  else
    echo "SKIPPED: no exported documents at $DATA (the export test must run first)."
  fi
else
  echo "SKIPPED: dotnet not found — the contract conformance gate was not run."
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$failures check(s) failed."
fi
exit "$failures"
