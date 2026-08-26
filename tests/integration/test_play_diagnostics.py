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

#: The log a `dotnet run` actually produces, and the reason the fallback skips warnings.
#:
#: NETSDK1206 is permanent (Noesis.GUI declares win10-* RIDs), irrelevant to every failure, and
#: FIRST -- so a fallback that took line 0 reported it as the diagnosis and sent the reader off to
#: fix an SDK warning that was never going to stop anything. The real cause is two lines down and
#: matches no failure marker, which is what puts it in the fallback's hands at all.
WARNING_PREAMBLE_THEN_PRECONDITION = """\
/usr/local/share/dotnet/sdk/10.0.203/Sdks/Microsoft.NET.Sdk/targets/Microsoft.NET.Sdk.targets(308,5): \
warning NETSDK1206: Found version-specific or distribution-specific runtime identifier(s): \
win10-arm, win10-x64, win10-x86. Affected libraries: Noesis.GUI. \
[/Users/x/ParadiseEngine/src/Paradise.Ui.Noesis/Paradise.Ui.Noesis.csproj]
[ShiningPie] Could not find a part of the path '/data/shiningpie/config.json'.
[ShiningPie] Working directory is '/'. Run from the repository root, or pass --scene/--config.
"""

#: A runtime's own fatal line that happens to contain the word "warning". It matches no failure
#: marker, so the fallback decides -- and skipping it would hand the reader the NEXT line, which
#: says less. This is why the noise check matches the MSBuild SHAPE (``": warning "``) rather than
#: the bare word.
RUNTIME_LINE_MENTIONING_WARNINGS = """\
[ShiningPie] Refusing to start: config.json is unreadable, ignoring warning suppressions.
[ShiningPie] Working directory is '/'.
"""

#: Nothing but noise. There is no good answer here, and the contract is that a bad one beats none:
#: the caller reports the exit code plus whatever this returns, and a silent failure is the thing
#: this whole extraction exists to prevent.
ONLY_WARNINGS = """\
/Users/x/A/A.csproj : warning NU1701: Package restored using .NETFramework.
/Users/x/B/B.csproj : warning NETSDK1206: Found version-specific runtime identifier(s).
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

    # THE REGRESSION. Both interesting lines are marker-free, so the marker scan declines and the
    # fallback decides -- and the first line is a build warning. Reporting it is what this pins
    # against: the fix is one branch, and without this case deleting that branch leaves the suite
    # green.
    check(
        "a build-warning preamble does not become the diagnosis",
        extract(WARNING_PREAMBLE_THEN_PRECONDITION),
        lambda line: (
            line == "[ShiningPie] Could not find a part of the path '/data/shiningpie/config.json'."
        ),
        "the missing-path line, not the NETSDK1206 warning",
    )

    # The skip is shaped, not lexical: a runtime line is kept even when it says "warning".
    check(
        "a runtime line mentioning warnings is not mistaken for build noise",
        extract(RUNTIME_LINE_MENTIONING_WARNINGS),
        lambda line: line is not None and line.startswith("[ShiningPie] Refusing to start"),
        "the refusal line",
    )

    # And the degenerate case the skip must not turn into silence.
    check(
        "a log of nothing but warnings still yields its first line",
        extract(ONLY_WARNINGS),
        lambda line: line is not None and "NU1701" in line,
        "the first warning rather than None",
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
