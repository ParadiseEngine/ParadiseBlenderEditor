"""Pin the diagnostic the Play operator reports when a launched runtime dies immediately.

The runtime is launched **detached**, with its stdout and stderr redirected to a log file, so
Blender never sees why it failed. Without this extraction every failure -- a build error, a
missing scene, a crash on the first frame -- appears in Blender as the same cheerful
``Launched Paradise runtime (pid N)``. Three real failures from wiring the addon up to
ShiningPie's launcher are pinned below, each of which was originally diagnosed by hand.

This lives in ``tests/integration`` rather than ``tests/unit`` because
``paradise_blender.play`` imports ``bpy`` at package import time, and the ``fake-bpy-module``
dev dependency ships PEP 561 stubs only -- there is no importable ``bpy`` outside Blender.

Run with::

    blender --background --factory-startup --python tests/integration/test_play_diagnostics.py

Exits non-zero on failure so CI can gate on it.
"""

from __future__ import annotations

import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from paradise_blender.play.host import first_error_line  # noqa: E402

MSBUILD_FAILURE = """\
  Determining projects to restore...
  Restored /Users/x/ShiningPie/ShiningPie.Core/ShiningPie.Core.csproj (in 412 ms).
/usr/local/share/dotnet/sdk/10.0.203/Microsoft.Common.CurrentVersion.targets(1844,5): \
error MSB3202: The project file "../../../ParadiseEngine/src/Paradise.Assets.Textures\
/Paradise.Assets.Textures.csproj" was not found.

The build failed. Fix the build errors and run again.
"""

LAUNCHER_PRECONDITION = """\
[ShiningPie] Could not find a part of the path '/data/shiningpie/config.json'.
[ShiningPie] Working directory is '/'. Run from the repository root, or pass --scene/--config.
"""

CRASH_AFTER_OUTPUT = """\
[ShiningPie] Loading scene...
[ShiningPie] 17 instances
Unhandled exception. System.NullReferenceException: Object reference not set.
   at ShiningPie.Game.Tick()
"""


def main() -> int:
    failures: list[str] = []
    directory = tempfile.mkdtemp(prefix="paradise_play_diag")
    log = os.path.join(directory, "paradise_play.log")

    def extract(body: str) -> str | None:
        with open(log, "w", encoding="utf-8") as handle:
            handle.write(body)
        return first_error_line(log)

    def check(label: str, actual, predicate, expectation: str) -> None:
        if predicate(actual):
            print(f"ok   {label}")
        else:
            failures.append(f"{label}: expected {expectation}, got {actual!r}")

    # A build log ends on "The build failed", which names no cause; the MSB error is 3 lines up.
    check(
        "build failure reports the MSB error, not the generic tail",
        extract(MSBUILD_FAILURE),
        lambda line: line is not None and "MSB3202" in line,
        "the line naming MSB3202",
    )

    # Neither line matches a failure marker. Reporting the *last* line would surface the generic
    # "run from the repository root" hint and hide which path was actually missing.
    check(
        "precondition failure reports the cause, not the trailing hint",
        extract(LAUNCHER_PRECONDITION),
        lambda line: (
            line == ("[ShiningPie] Could not find a part of the path '/data/shiningpie/config.json'.")
        ),
        "the missing-path line",
    )

    # Here the first line is ordinary output, so only the marker scan gets this right.
    check(
        "crash below normal output reports the exception banner",
        extract(CRASH_AFTER_OUTPUT),
        lambda line: line is not None and line.startswith("Unhandled exception."),
        "the unhandled-exception line",
    )

    check(
        "blank lines are skipped",
        extract("\n\n   \n[ShiningPie] boom\n"),
        lambda line: line == "[ShiningPie] boom",
        "the only non-empty line",
    )

    check(
        "an overlong line is truncated for the status bar",
        extract("error " + "x" * 500),
        lambda line: line is not None and len(line) == 300 and line.endswith("..."),
        "300 characters ending in an ellipsis",
    )

    # None, not "", so the caller can fall back to reporting the bare exit code.
    check(
        "an empty log yields no diagnostic",
        extract(""),
        lambda line: line is None,
        "None",
    )

    check(
        "a missing log yields no diagnostic",
        first_error_line(os.path.join(directory, "absent.log")),
        lambda line: line is None,
        "None",
    )

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nPlay diagnostics passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
