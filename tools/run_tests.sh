#!/usr/bin/env bash
# Full test suite: unit tests, then the Blender integration tests.
#
# The two layers check genuinely different things:
#   unit         the document format and the axis math against themselves (fast, no Blender)
#   integration  the axis conversion against Blender's OWN glTF exporter, and every operator
#                that touches a real project: open, save, extract, thumbnails, play
#
# Only the second can catch a wrong axis convention -- the unit tests would happily pass with
# the basis inverted, because they only check the conversion against itself.
#
# There is no .NET layer any more. It existed to round-trip a SECOND, Python implementation of
# the export contract through the engine's own reader and writer; that implementation was the
# `.blend`-is-truth addon, and it is gone (#35). What builds documents now is the engine's own
# C# pipeline, so the check would read back what it had just written.

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

# One Blender integration test. Everything after the label is passed to the script after `--`.
# The greps drop Blender's own startup chatter, which otherwise buries the result.
integration() {
  local label="$1" script="$2" noise="$3"
  shift 3
  # `${extra[@]+...}`, not `${extra[@]}`: macOS ships bash 3.2, where an empty array read under
  # `set -u` is an unbound variable and would abort the run.
  local extra=()
  if [ "$#" -gt 0 ]; then extra=(-- "$@"); fi

  step "Integration: $label"
  "$BLENDER" --background --factory-startup --python-exit-code 1 --python "$script" \
    ${extra[@]+"${extra[@]}"} 2>&1 | grep -vE "$noise" | tail -30
  check "${PIPESTATUS[0]}" "$label"
}

DEFAULT_NOISE='^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2})'

step "Unit tests (document format and axis math, no Blender)"
"$PYTHON" -m pytest tests/unit -q
check $? "unit tests"

if command -v "$BLENDER" >/dev/null 2>&1; then
  # THE test: our conversion against Blender's own glTF exporter. A wrong basis rotates every
  # mesh in every document by 90 degrees and nothing else here would notice.
  integration "axis parity vs Blender's glTF exporter" \
    tests/integration/test_axis_parity.py "$DEFAULT_NOISE"

  # The sidebar: which panels are available when, and that every operator they draw exists.
  integration "the sidebar panels" \
    tests/integration/test_panel.py \
    '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|Info: Saved)'

  # The Outliner and viewport right-click entries, and the load-time tag the first one needs.
  integration "the object context menus" \
    tests/integration/test_context_menu.py "$DEFAULT_NOISE"

  # Groups: a document object with only meta and transform, shown as a Blender collection.
  integration "groups as collections" \
    tests/integration/test_groups.py "$DEFAULT_NOISE"

  # The byte-exact round trip through Blender needs a real asset project; it skips cleanly when
  # PARADISE_ASSETS_PROJECT names nothing.
  integration "open and save an asset-project scene" \
    tests/integration/test_open_scene.py "$DEFAULT_NOISE" \
    "${PARADISE_ASSETS_PROJECT:-../shiningpie}"

  # Creating prefabs: extraction, and the model prefab seed. It COPIES the project first --
  # these operators write and delete, so the checkout must never be the thing under test.
  integration "extract to prefab, and the model prefab seed" \
    tests/integration/test_create_prefab.py \
    '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|Info: |.*\| Saved:)' \
    "${PARADISE_ASSETS_PROJECT:-../shiningpie}"

  # Asset Browser thumbnails. Renders, so it wants the same real project -- and its load-bearing
  # check is that the catalogue came out with no geometry in it.
  integration "prefab thumbnails and the catalogue's weight" \
    tests/integration/test_prefab_thumbnails.py \
    '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|.*\| Saved:)' \
    "${PARADISE_ASSETS_PROJECT:-../shiningpie}"

  integration "the Asset Browser context menu and its sidecar" \
    tests/integration/test_asset_browser_menu.py \
    '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|.*\| (Saved|Read blend):)' \
    "${PARADISE_ASSETS_PROJECT:-../shiningpie}"

  # Build & Play, against FAKE tools -- it needs no project, no CLI and no game.
  integration "build and play, up to the process" \
    tests/integration/test_play.py "$DEFAULT_NOISE"

  # Blender's own save writing the document. Its own throwaway project -- every check writes.
  integration "Ctrl+S writes the prefab document" \
    tests/integration/test_save_on_save.py \
    '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|Info: Saved)'

  # Opening a cached .blend must rematerialize from assets/ and start the watcher. Own project.
  integration "opening a cached .blend refreshes from assets" \
    tests/integration/test_open_workfile.py \
    '^(INFO|[0-9]{2}:[0-9]{2}:[0-9]{2}|Info: Saved|.*Read blend)'
else
  echo "SKIPPED: Blender not found (set BLENDER=/path/to/blender) — integration tests not run."
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$failures check(s) failed."
fi
exit "$failures"
