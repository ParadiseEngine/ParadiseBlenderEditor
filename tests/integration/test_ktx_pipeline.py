"""Pin the KTX2 transcoder's two argument dialects and its colour/data classification.

Both failure modes here are silent. Passing toktx's argument order to `ktx create` makes it read
the destination as an input image; encoding a normal map as sRGB bakes the transfer function into
the numbers and lights the model wrongly. Neither produces an error a user would connect to the
texture pipeline -- the first looks like a broken file, the second like a shader bug.

Lives in tests/integration because `paradise_blender.pipeline` imports `bpy` at package import
time and fake-bpy-module ships PEP 561 stubs only.

Run with::

    blender --background --factory-startup --python tests/integration/test_ktx_pipeline.py

Exits non-zero on failure so CI can gate on it.
"""

from __future__ import annotations

import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from paradise_blender.pipeline import ktx  # noqa: E402

COLOUR = [
    "T_Superhero_Male_Dark.png",
    "T_Hair_1_BaseColor.png",
    "albedo.png",
    "T_Eye_Brown.png",
]
DATA = [
    "T_Superhero_Male_Normal.png",
    "T_Superhero_Male_Roughness.png",
    "T_Eye_Normal.png",
    "hero_normal_map.png",
    "body-roughness.png",
    "character_ORM.png",
]


def main() -> int:
    failures: list[str] = []

    def check(label: str, actual, expected) -> None:
        if actual == expected:
            print(f"ok   {label}")
        else:
            failures.append(f"{label}: expected {expected!r}, got {actual!r}")

    # --- classification -------------------------------------------------------------------
    for name in COLOUR:
        check(f"colour: {name}", ktx.is_linear(name), False)
    for name in DATA:
        check(f"data:   {name}", ktx.is_linear(name), True)

    # "normalcy" must not match "normal" -- tokens are compared whole, not as substrings.
    check("token boundary: Normalcy_BaseColor.png", ktx.is_linear("Normalcy_BaseColor.png"), False)

    # Normal maps are a STRICT SUBSET of the linear set: every other data map (roughness, ORM,
    # AO) is linear but single-purpose, and must NOT get the two-channel normal encoding.
    check("normal: T_Hero_Normal.png", ktx.is_normal_map("T_Hero_Normal.png"), True)
    check("normal: hero_nrm.png", ktx.is_normal_map("hero_nrm.png"), True)
    check("not normal: body-roughness.png", ktx.is_normal_map("body-roughness.png"), False)
    check("not normal: character_ORM.png", ktx.is_normal_map("character_ORM.png"), False)
    check("not normal: T_Hero_BaseColor.png", ktx.is_normal_map("T_Hero_BaseColor.png"), False)

    # --- dialect detection ----------------------------------------------------------------
    check("dialect: /usr/local/bin/ktx", ktx._is_modern("/usr/local/bin/ktx"), True)
    check("dialect: /opt/KTX/bin/toktx", ktx._is_modern("/opt/KTX/bin/toktx"), False)

    # --- argument order -------------------------------------------------------------------
    # The whole point of the fix: the two tools take source and target in OPPOSITE order.
    captured: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""

    original_run = ktx.subprocess.run
    ktx.subprocess.run = lambda command, **_kwargs: (captured.append(command), _Result())[1]
    try:
        directory = tempfile.mkdtemp(prefix="paradise_ktx_test")
        source = os.path.join(directory, "T_Hero_Normal.png")
        colour_source = os.path.join(directory, "T_Hero_BaseColor.png")
        for path in (source, colour_source):
            with open(path, "wb") as handle:
                handle.write(b"not-a-real-png")
        target = os.path.splitext(source)[0] + ".ktx2"

        captured.clear()
        ktx.convert_image(source, ktx.Transcoder("/usr/local/bin/ktx", modern=True), force=True)
        modern = captured[0]
        check("modern: subcommand", modern[1], "create")
        check("modern: source precedes target", modern.index(source) < modern.index(target), True)
        check("modern: normal map is UNORM", modern[modern.index("--format") + 1], "R8G8B8A8_UNORM")
        # Linear alone leaves the encoder in plain-RGB mode, which the runtime transcoder
        # misreads as its two-channel RRRG layout -- the model renders DARK, not broken.
        check("modern: normal map gets --normal-mode", "--normal-mode" in modern, True)
        # Without --assign-tf, `ktx create` assumes untagged 8-bit PNGs are sRGB and silently
        # linearizes the pixels to match the UNORM format -- flat normal 128 becomes ~55 and
        # the model renders uniformly dark. The warning it prints is captured and discarded.
        check("modern: normal map pins --assign-tf linear", modern[modern.index("--assign-tf") + 1], "linear")

        captured.clear()
        ktx.convert_image(colour_source, ktx.Transcoder("/usr/local/bin/ktx", modern=True), force=True)
        check(
            "modern: basecolor is SRGB",
            captured[0][captured[0].index("--format") + 1],
            "R8G8B8A8_SRGB",
        )
        check("modern: basecolor has no --normal-mode", "--normal-mode" in captured[0], False)
        check("modern: basecolor pins --assign-tf srgb", captured[0][captured[0].index("--assign-tf") + 1], "srgb")

        captured.clear()
        ktx.convert_image(source, ktx.Transcoder("/opt/bin/toktx", modern=False), force=True)
        legacy = captured[0]
        check("legacy: target precedes source", legacy.index(target) < legacy.index(source), True)
        check("legacy: no `create` subcommand", "create" in legacy, False)
        check("legacy: no --format", "--format" in legacy, False)
    finally:
        ktx.subprocess.run = original_run

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nKTX pipeline checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
