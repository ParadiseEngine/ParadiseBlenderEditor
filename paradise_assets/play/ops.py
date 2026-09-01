"""Build, Play, Verify and Clean -- the CLI's verbs, as buttons.

**Play builds first, and a failed build stops it.** That is the difference from
``paradise_blender``'s Play, which deliberately never exports because ``data/`` is kept fresh by a
save hook. Nothing keeps ``.editor/play/`` fresh: ``assets/`` is source, the runtime reads built
trees only, and the CLI is the thing in between. A Play that skipped the build would launch
whatever the last one left -- showing the author a level they edited an hour ago with nothing on
screen saying so -- and a Play that built but launched anyway on failure would do the same thing
while looking like it had worked.

Which document plays is the one that is OPEN. Every authoring document is a prefab and a prefab can
be played (the asset plan's §2.9), so this needs no notion of a "main level" and a prop opened on
its own is as launchable as a district.
"""

from __future__ import annotations

import json
import os
import time

import bpy
from bpy.props import BoolProperty
from bpy.types import Operator

from ..document import project
from ..materialize import store
from .host import (
    first_error_line,
    launch_runtime,
    log_path,
    resolve_cli_command,
    resolve_runtime_command,
    run_cli,
)

__all__ = ["classes"]

# How long to keep watching a launched runtime for an early death.
#
# THREE MINUTES, because thirty seconds was not enough in the module this was ported from, and the
# way it failed was the worst available: the watch expired, the operator reported success, and the
# build died a minute later with nobody listening. "Play does nothing and says nothing" is far
# harder to diagnose than any error message, and it is what this constant produces when too small.
#
# Waiting is free -- the timer polls a `poll()` and passes every other event through -- and launch
# success is reported immediately rather than at the end of the watch, so a long window costs the
# author no perceived latency either.
#
# IT BOUNDS THE FAILURE RATHER THAN REMOVING IT. The architecture is still "no death within the
# window means success", so a runtime slower than three minutes reports success and then dies
# unheard. Removing it properly means watching until the process either dies or is observed to have
# opened a window, and Blender cannot see the second half.
WATCH_SECONDS = 180.0
POLL_INTERVAL = 0.4

#: What the CLI is called in messages, so the fix is always spelled the same way.
CLI_MISSING = (
    "No Paradise CLI found. Set 'Paradise CLI' in the addon preferences to the `paradise` "
    "executable or to Paradise.Cli.csproj, or install it with `dotnet tool install -g`."
)


def _project(operator) -> tuple[project.ProjectLayout, str] | None:
    """The open document's project and path, reporting why not when there is none."""
    state = store.read_state(bpy.context.scene)
    if state is None:
        operator.report({"ERROR"}, "No prefab document is open")
        return None

    layout = project.locate(state.path)
    if layout is None:
        operator.report({"ERROR"}, f"No asset project at or above {state.path}")
        return None
    return layout, state.path


def _profile() -> str:
    """Which build profile to use. Through ``host`` so every preference read has one seam."""
    from .host import _preference

    return _preference("build_profile", "dev")


def _run(operator, layout, arguments: list[str], verb: str) -> bool:
    """Run one CLI verb, reporting its own words rather than a generic failure."""
    if resolve_cli_command() is None:
        operator.report({"ERROR"}, CLI_MISSING)
        return False

    result = run_cli(arguments, cwd=layout.root)
    if result is None:
        operator.report({"ERROR"}, CLI_MISSING)
        return False

    if not result.ok:
        operator.report({"ERROR"}, f"{verb} failed — {result.summary()}")
        return False
    return True


def _built_scene(layout, document_path: str) -> str:
    """Where the CLI puts this document's compiled form in the play tree.

    ``assets/levels/x.prefab`` -> ``.editor/play/levels/x.prefab``: play bakes the document but
    keeps the authoring name, so this is the source-relative path under the play tree.
    """
    relative = os.path.relpath(document_path, layout.assets)
    return os.path.join(layout.editor, "play", relative)


def _derived_config(play_root: str) -> list[str]:
    """``--config`` for games that keep one where the build puts it, and nothing otherwise.

    The build copies the project's own folder through under its manifest name, so a game whose
    config lives at ``assets/<name>/config.toml`` has it at ``<play>/<name>/config.toml`` (or
    ``.json``, if that is the profile). The file is passed only when it is actually there, and a
    game that arranges things differently says so through the Runtime Arguments preference rather
    than being second-guessed here.
    """
    manifest = os.path.join(play_root, "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as handle:
            name = json.load(handle).get("project")
    except (OSError, ValueError, AttributeError):
        return []

    if not isinstance(name, str) or not name:
        return []
    directory = os.path.join(play_root, name)
    for extension in (".toml", ".json"):
        config = os.path.join(directory, "config" + extension)
        if os.path.isfile(config):
            return ["--config", config]
    return []


class PARADISE_ASSETS_OT_play(Operator):
    """Build this project into .editor/play and launch the game on the open document"""

    bl_idname = "paradise_assets.play"
    bl_label = "Build & Play"
    bl_options = {"REGISTER"}

    # Plain Python attributes, not RNA properties: they hold the watch state for one modal run and
    # must not be saved, shown in the redo panel, or set from a keymap.
    _process = None
    _timer = None
    _deadline = 0.0

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def execute(self, context):
        found = _project(self)
        if found is None:
            return {"CANCELLED"}
        layout, document_path = found

        if not _run(self, layout, ["assets", "build", "--profile", _profile(), "--editor"], "Build"):
            return {"CANCELLED"}

        scene_path = _built_scene(layout, document_path)
        if not os.path.isfile(scene_path):
            # The build succeeded and did not produce this. Most likely the document is not one
            # the profile emits; say which file was expected rather than "something went wrong".
            self.report(
                {"ERROR"},
                f"The build succeeded but {os.path.relpath(scene_path, layout.root)} is not there.",
            )
            return {"CANCELLED"}

        play_root = os.path.join(layout.editor, "play")
        arguments = ["--scene", scene_path, *_derived_config(play_root)]

        # IN THE PROJECT ROOT, not wherever Blender happens to be. --scene is absolute so the
        # level is always found; everything else a runtime reads is conventionally relative to
        # the project root. See launch_runtime for the failure this prevents.
        process, error = launch_runtime(arguments, cwd=layout.root)
        if process is None:
            self.report({"ERROR"}, error or "Could not launch the runtime")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Launched {os.path.basename(document_path)} (pid {process.pid})")

        # Background Blender has no event loop to drive a modal, so a timer added here would never
        # fire and the operator would sit in RUNNING_MODAL forever. Launching still succeeded, so
        # report success rather than failing a scripted call over a diagnostic it could not show.
        #
        # `bpy.app.background` is the test, NOT `context.window is None`: 5.2 hands a background
        # run a window in its context, so the window check alone passes straight into the modal
        # path and a scripted `bpy.ops.paradise_assets.play()` never returns FINISHED.
        if bpy.app.background or context.window is None:
            return {"FINISHED"}

        # Watch the child instead of declaring victory on a successful fork: a detached runtime
        # writes to a log, so a missing asset would otherwise show up in Blender as a pid and
        # nothing else.
        self._process = process
        self._deadline = time.monotonic() + WATCH_SECONDS
        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(POLL_INTERVAL, window=context.window)
        window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        code = self._process.poll()
        if code is None:
            # Still alive past the window: it opened, and its lifetime is the player's business.
            return self._release(context) if time.monotonic() >= self._deadline else {"PASS_THROUGH"}

        if code == 0:
            return self._release(context)

        detail = first_error_line(log_path())
        self.report(
            {"ERROR"},
            f"The game exited with code {code}: {detail}"
            if detail
            else f"The game exited with code {code} — see {log_path()}",
        )
        self._release(context)
        return {"CANCELLED"}

    def cancel(self, context) -> None:
        self._release(context)

    def _release(self, context) -> set[str]:
        """Drop the timer. Safe to call twice -- cancel also runs on a modal that finished."""
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        return {"FINISHED"}


class PARADISE_ASSETS_OT_build(Operator):
    """Compile assets/ into build/ with the configured profile"""

    bl_idname = "paradise_assets.build"
    bl_label = "Build"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def execute(self, context):
        found = _project(self)
        if found is None:
            return {"CANCELLED"}
        layout, _ = found

        profile = _profile()
        if not _run(self, layout, ["assets", "build", "--profile", profile], "Build"):
            return {"CANCELLED"}

        self.report({"INFO"}, f"Built '{profile}' into {os.path.join(layout.root, 'build')}")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_verify(Operator):
    """Check the assets tree: sidecars, identities, document validity"""

    bl_idname = "paradise_assets.verify"
    bl_label = "Verify"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def execute(self, context):
        found = _project(self)
        if found is None:
            return {"CANCELLED"}
        layout, _ = found

        if resolve_cli_command() is None:
            self.report({"ERROR"}, CLI_MISSING)
            return {"CANCELLED"}

        result = run_cli(["assets", "verify"], cwd=layout.root)
        if result is None:
            self.report({"ERROR"}, CLI_MISSING)
            return {"CANCELLED"}

        # A non-zero exit here means the TREE has errors, not that the tool failed -- so the
        # findings are the result, reported as warnings, and the operator still finishes. Each
        # finding names a file and a reason; showing a handful beats showing a count.
        findings = [
            line.strip() for line in result.stdout.splitlines()
            if line.strip().startswith(("error:", "warning:"))
        ]
        for finding in findings[:5]:
            self.report({"WARNING"}, finding)

        summary = next(
            (line.strip() for line in result.stdout.splitlines() if line.startswith("verify:")),
            result.summary(),
        )
        self.report({"WARNING"} if findings else {"INFO"}, summary)
        return {"FINISHED"}


class PARADISE_ASSETS_OT_clean(Operator):
    """Delete derived output. Keeps .editor/ unless you ask otherwise"""

    bl_idname = "paradise_assets.clean"
    bl_label = "Clean"
    bl_options = {"REGISTER"}

    #: Off by default, and it is the only destructive thing in this module.
    #:
    #: ``.editor/`` holds the Asset Browser catalogue, its rendered thumbnails, and every working
    #: file -- the per-document ``.blend`` carrying camera, selection and anything parented to
    #: nothing. All of it is regenerable, which is exactly why deleting it is easy to do without
    #: thinking and annoying to have done: the next open re-imports every GLB and the next
    #: catalogue re-renders every thumbnail.
    editor_too: BoolProperty(  # type: ignore[valid-type]
        name="Also delete .editor/",
        description=(
            "Delete the editor cache too: the Asset Browser catalogue, its thumbnails and every "
            "document's working file. All regenerable, all slow to regenerate"
        ),
        default=False,
    )

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, _context) -> None:
        layout = self.layout
        layout.label(text="Delete build/ for this project?", icon="TRASH")
        layout.prop(self, "editor_too")
        if self.editor_too:
            layout.label(text="The catalogue, thumbnails and working files go too.", icon="ERROR")

    def execute(self, context):
        found = _project(self)
        if found is None:
            return {"CANCELLED"}
        layout, _ = found

        arguments = ["assets", "clean"] + ([] if self.editor_too else ["--keep-editor"])
        if not _run(self, layout, arguments, "Clean"):
            return {"CANCELLED"}

        self.report({"INFO"}, "Cleaned build/" + (" and .editor/" if self.editor_too else ""))
        return {"FINISHED"}


def status() -> list[tuple[str, str]]:
    """``(icon, message)`` for each part of the toolchain that is not ready.

    Resolves without logging, because the panel asks on every redraw.
    """
    from .host import _preference

    problems: list[tuple[str, str]] = []
    if resolve_cli_command() is None:
        problems.append(("ERROR", "No Paradise CLI — set it in preferences"))
    if resolve_runtime_command() is None:
        problems.append(("ERROR", "No runtime host — set it in preferences"))

    # Said out loud because the alternative is silent. The pipeline resolves PARADISE_KTX_PATH
    # with File.Exists, so a path that is a directory -- or a typo, or a moved install -- is
    # discarded rather than rejected, and the only symptom is a build that cannot encode a
    # texture. A field that LOOKS filled in is worse than an empty one.
    ktx = _preference("ktx_path").strip()
    if ktx and not os.path.isfile(os.path.expanduser(ktx)):
        problems.append(("ERROR", "KTX path is not a file — point it at ktx.exe itself"))
    return problems


classes = (
    PARADISE_ASSETS_OT_play,
    PARADISE_ASSETS_OT_build,
    PARADISE_ASSETS_OT_verify,
    PARADISE_ASSETS_OT_clean,
)
