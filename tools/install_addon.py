#!/usr/bin/env python3
"""Link this working copy into Blender's extensions directory.

Symlinks rather than copies, so editing the source here takes effect on the next Blender
restart (or addon reload) with no reinstall step. That is the whole point during development;
for distribution, build a proper extension zip -- from the package directory, which is
where ``blender_manifest.toml`` lives and what Blender treats as the extension root::

    mkdir -p dist
    blender --command extension build --source-dir paradise_blender --output-dir dist

Pointing ``--source-dir`` at the repository root fails: there is no ``__init__.py``
there, and the manifest is deliberately not there either.

Usage::

    python3 tools/install_addon.py            # link
    python3 tools/install_addon.py --remove   # unlink
    python3 tools/install_addon.py --list     # show candidate extension directories

Blender must be restarted after linking, then the addon enabled in
Preferences > Add-ons. Linking prints which one to enable.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys

#: The extensions this repository ships. Both are linked by default, and both can be enabled at
#: once -- they are complements during the migration, not alternatives:
#:
#:   paradise_blender  the .blend is the source of truth and exports to data/
#:   paradise_assets   assets/ is the source of truth and the .blend is a cache of one scene
#:
#: Pass a package name to link just one.
PACKAGES = ("paradise_blender", "paradise_assets")

#: How each appears in Preferences > Add-ons, and what it is for.
DISPLAY_NAMES = {
    "paradise_blender": "'Paradise Engine Tools'  — the .blend is the source, exported to data/",
    "paradise_assets": "'Paradise Assets'        — open assets/levels/*.prefab and place things in it",
}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def extension_roots() -> list[str]:
    """Candidate ``extensions/user_default`` directories, newest Blender first.

    Blender 4.2+ keeps user extensions here; the layout differs per platform, and several
    Blender versions can be installed side by side.
    """
    system = platform.system()
    if system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support/Blender")
    elif system == "Windows":
        base = os.path.join(os.environ.get("APPDATA", ""), "Blender Foundation", "Blender")
    else:
        base = os.path.expanduser("~/.config/blender")

    if not os.path.isdir(base):
        return []

    versions = sorted(
        (entry for entry in os.listdir(base) if entry[:1].isdigit()),
        key=_version_key,
        reverse=True,
    )
    return [os.path.join(base, version, "extensions", "user_default") for version in versions]


def _version_key(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return (0,)


def install(remove: bool, packages: tuple[str, ...] = PACKAGES) -> int:
    """Link (or unlink) each package, reporting the worst outcome.

    Each is independent: one failing to link should not stop the other, because the common
    reason for a failure is a real directory already installed under that name.
    """
    roots = extension_roots()
    if not roots:
        print("No Blender configuration directory found. Run Blender once first.", file=sys.stderr)
        return 1

    return max(_install_one(package, roots[0], remove) for package in packages)


def _install_one(package: str, target_root: str, remove: bool) -> int:
    source = os.path.join(REPO, package)
    if not os.path.isdir(source):
        print(f"'{source}' does not exist — run this from the repository.", file=sys.stderr)
        return 1

    target = os.path.join(target_root, package)

    if remove:
        if os.path.islink(target):
            os.unlink(target)
            print(f"Unlinked {target}")
        elif os.path.isdir(target):
            # Refuse to delete a real directory: it may be a genuine installed copy the user
            # wants, and removing it silently would be destructive.
            print(
                f"'{target}' is a real directory, not a symlink from this script. "
                "Remove it manually if that is what you intend.",
                file=sys.stderr,
            )
            return 1
        else:
            print(f"Nothing linked at {target}")
        return 0

    os.makedirs(target_root, exist_ok=True)

    if os.path.islink(target):
        existing = os.path.realpath(target)
        if existing == os.path.realpath(source):
            print(f"Already linked: {target} -> {source}")
            return 0
        os.unlink(target)
    elif os.path.exists(target):
        print(
            f"'{target}' already exists and is not a symlink. Remove it first if you want to "
            "link this working copy.",
            file=sys.stderr,
        )
        return 1

    os.symlink(source, target, target_is_directory=True)
    print(f"Linked {target} -> {source}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="unlink instead of linking")
    parser.add_argument("--list", action="store_true", help="list candidate extension directories")
    parser.add_argument(
        "package",
        nargs="*",
        metavar="PACKAGE",
        help=f"which extension(s) to link (default: all of {', '.join(PACKAGES)})",
    )
    args = parser.parse_args()

    # Validated here rather than through `choices`, which with nargs="*" needs the empty list
    # smuggled into the choices to accept "no argument" and then prints that in the help.
    unknown = [name for name in args.package if name not in PACKAGES]
    if unknown:
        parser.error(f"unknown package(s) {', '.join(unknown)}; expected one of {', '.join(PACKAGES)}")

    if args.list:
        roots = extension_roots()
        if not roots:
            print("(none found)")
        for root in roots:
            marker = "*" if root == roots[0] else " "
            print(f"{marker} {root}")
        return 0

    linked = tuple(args.package) or PACKAGES
    status = install(args.remove, linked)
    if status == 0 and not args.remove:
        # Only what was actually linked: naming the other one sends someone looking through
        # Preferences for an add-on this run did not install.
        print("\nRestart Blender, then enable it in Preferences > Add-ons:")
        for package in linked:
            print(f"  {DISPLAY_NAMES[package]}")
    return status


if __name__ == "__main__":
    sys.exit(main())
