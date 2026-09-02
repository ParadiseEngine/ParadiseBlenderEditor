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
# A venv puts its interpreter in `bin` on POSIX and `Scripts` on Windows, so both are tried
# before falling back. The fallback matters on Windows in particular: `python3` there is usually
# the Microsoft Store's stub, which prints an install advert and exits non-zero -- so without the
# Scripts branch this reported "FAILED: unit tests" on a machine where every test passes.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in .venv/bin/python .venv/Scripts/python.exe; do
    [ -x "$candidate" ] && PYTHON="$candidate" && break
  done
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || PYTHON="python3"

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

  # paradise_assets: the OTHER addon, which opens assets/**/*.prefab rather than exporting
  # to data/. Its gate is a byte-exact round trip through Blender, so it needs a real asset
  # project -- it skips cleanly when PARADISE_ASSETS_PROJECT names nothing.
  step "Integration: open and save an asset-project scene"
  "$BLENDER" --background --factory-startup --python tests/integration/test_open_scene.py -- \
    "${PARADISE_ASSETS_PROJECT:-../shiningpie}" 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -30
  check "${PIPESTATUS[0]}" "open scene"

  # Asset Browser thumbnails. Renders, so it wants the same real project -- and its load-bearing
  # check is that the catalogue came out with no geometry in it.
  step "Integration: prefab thumbnails and the catalogue's weight"
  "$BLENDER" --background --factory-startup --python tests/integration/test_prefab_thumbnails.py -- \
    "${PARADISE_ASSETS_PROJECT:-../shiningpie}" 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|.*\| Saved:)' | tail -30
  check "${PIPESTATUS[0]}" "prefab thumbnails"

  step "Integration: the Asset Browser context menu and its sidecar"
  "$BLENDER" --background --factory-startup --python tests/integration/test_asset_browser_menu.py -- \
    "${PARADISE_ASSETS_PROJECT:-../shiningpie}" 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|.*\| (Saved|Read blend):)' | tail -30
  check "${PIPESTATUS[0]}" "asset browser menu"

  # Build & Play, against FAKE tools -- it needs no project, no CLI and no game.
  step "Integration: build and play, up to the process"
  "$BLENDER" --background --factory-startup --python tests/integration/test_play.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})' | tail -30
  check "${PIPESTATUS[0]}" "build and play"

  # Blender's own save writing the document. Its own throwaway project -- every check writes.
  step "Integration: Ctrl+S writes the prefab document"
  "$BLENDER" --background --factory-startup --python tests/integration/test_save_on_save.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|Info: Saved)' | tail -30
  check "${PIPESTATUS[0]}" "save on save"

  # Opening a cached .blend must rematerialize from assets/ and start the watcher. Own project.
  step "Integration: opening a cached .blend refreshes from assets"
  "$BLENDER" --background --factory-startup --python tests/integration/test_open_workfile.py 2>&1 \
    | grep -vE '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|Info: Saved|.*Read blend)' | tail -30
  check "${PIPESTATUS[0]}" "open workfile"
else
  echo "SKIPPED: Blender not found (set BLENDER=/path/to/blender) — integration tests not run."
fi

if command -v dotnet >/dev/null 2>&1; then
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
