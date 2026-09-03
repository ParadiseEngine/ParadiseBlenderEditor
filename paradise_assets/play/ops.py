"""Build, Play, Verify and Clean as buttons. Play builds first and a failed build stops it:
nothing else keeps ``.editor/play/`` fresh, and launching anyway would show an hour-old level
with nothing on screen saying so. The open document plays; every prefab is playable (§2.9).
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
    schema_build_stage,
    start_cli,
    start_schema_build,
)

__all__ = ["classes"]

# Three minutes: thirty seconds was not enough, and the failure was the worst kind (watch
# expired, success reported, build died a minute later unheard). Waiting is free. It bounds
# the failure rather than removing it: Blender cannot see whether a window opened.
WATCH_SECONDS = 180.0
POLL_INTERVAL = 0.4

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


def _modal_possible(context) -> bool:
    """Whether a modal timer can drive this operator. ``bpy.app.background``, NOT
    ``context.window is None``: 5.2 hands a background run a window, and a scripted operator
    would never return FINISHED."""
    return not bpy.app.background and context.window is not None


class _CliOperator:
    """A CLI verb run from a modal timer so the UI stays live (#36). Subclasses set ``verb``
    and ``arguments`` and implement ``finished``; ``execute`` falls back to the blocking run
    when there is no event loop."""

    verb = ""
    _job = None
    _timer = None
    _layout = None

    def cli_arguments(self) -> list[str]:
        raise NotImplementedError

    def finished(self, context, result) -> set[str]:
        """The CLI ran; ``result.ok`` says how. Return the operator's own status."""
        raise NotImplementedError

    def execute(self, context):
        found = _project(self)
        if found is None:
            return {"CANCELLED"}
        self._layout, self._document_path = found

        if resolve_cli_command() is None:
            self.report({"ERROR"}, CLI_MISSING)
            return {"CANCELLED"}

        if not _modal_possible(context):
            result = run_cli(self.cli_arguments(), cwd=self._layout.root)
            if result is None:
                self.report({"ERROR"}, CLI_MISSING)
                return {"CANCELLED"}
            return self.finished(context, result)

        self._job = start_cli(self.cli_arguments(), cwd=self._layout.root)
        if self._job is None:
            self.report({"ERROR"}, CLI_MISSING)
            return {"CANCELLED"}

        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(POLL_INTERVAL, window=context.window)
        window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        result = self._job.poll()
        if result is None:
            return {"PASS_THROUGH"}
        self._drop_timer(context)
        return self.finished(context, result)

    def cancel(self, context) -> None:
        if self._job is not None:
            self._job.cancel()
        self._drop_timer(context)

    def _drop_timer(self, context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    def _report_failure(self, result) -> set[str]:
        self.report({"ERROR"}, f"{self.verb} failed — {result.summary()}")
        return {"CANCELLED"}


def _built_scene(layout, document_path: str) -> str:
    """``assets/levels/x.prefab`` -> ``.editor/play/levels/x.prefab`` (play keeps the name)."""
    relative = os.path.relpath(document_path, layout.assets)
    return os.path.join(layout.editor, "play", relative)


def _derived_config(play_root: str) -> list[str]:
    """``--config`` when ``<play>/<name>/config.{toml,json}`` exists, nothing otherwise; a game
    arranged differently says so through the Runtime Arguments preference."""
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


class PARADISE_ASSETS_OT_play(_CliOperator, Operator):
    """Build this project into .editor/play and launch the game on the open document"""

    bl_idname = "paradise_assets.play"
    bl_label = "Build & Play"
    bl_options = {"REGISTER"}
    verb = "Build"

    # Plain attributes, not RNA properties: watch state must not reach the redo panel or a keymap.
    _process = None
    _deadline = 0.0

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def cli_arguments(self) -> list[str]:
        return ["assets", "build", "--profile", _profile(), "--editor"]

    def modal(self, context, event):
        if self._process is None:
            return _CliOperator.modal(self, context, event)
        return self._watch_runtime(context, event)

    def finished(self, context, result) -> set[str]:
        if not result.ok:
            return self._report_failure(result)
        layout, document_path = self._layout, self._document_path

        scene_path = _built_scene(layout, document_path)
        if not os.path.isfile(scene_path):
            # Name the expected file rather than "something went wrong".
            self.report(
                {"ERROR"},
                f"The build succeeded but {os.path.relpath(scene_path, layout.root)} is not there.",
            )
            return {"CANCELLED"}

        play_root = os.path.join(layout.editor, "play")
        arguments = ["--scene", scene_path, *_derived_config(play_root)]

        # In the project root, not wherever Blender is (see launch_runtime).
        process, error = launch_runtime(arguments, cwd=layout.root)
        if process is None:
            self.report({"ERROR"}, error or "Could not launch the runtime")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Launched {os.path.basename(document_path)} (pid {process.pid})")

        # Background Blender has no event loop, so a modal would sit in RUNNING_MODAL forever.
        if not _modal_possible(context):
            return {"FINISHED"}

        # A detached runtime's death would otherwise show up as a pid and nothing else. The
        # build's modal handler is still registered; it now watches the runtime instead.
        self._process = process
        self._deadline = time.monotonic() + WATCH_SECONDS
        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(POLL_INTERVAL, window=context.window)
        return {"RUNNING_MODAL"}

    def _watch_runtime(self, context, event):
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
        _CliOperator.cancel(self, context)

    def _release(self, context) -> set[str]:
        """Drop the timer. Safe to call twice -- cancel also runs on a modal that finished."""
        self._drop_timer(context)
        return {"FINISHED"}


class PARADISE_ASSETS_OT_build(_CliOperator, Operator):
    """Compile assets/ into build/ with the configured profile"""

    bl_idname = "paradise_assets.build"
    bl_label = "Build"
    bl_options = {"REGISTER"}
    verb = "Build"

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def cli_arguments(self) -> list[str]:
        return ["assets", "build", "--profile", _profile()]

    def finished(self, context, result) -> set[str]:
        if not result.ok:
            return self._report_failure(result)
        self.report(
            {"INFO"}, f"Built '{_profile()}' into {os.path.join(self._layout.root, 'build')}")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_verify(_CliOperator, Operator):
    """Check the assets tree: sidecars, identities, document validity"""

    bl_idname = "paradise_assets.verify"
    bl_label = "Verify"
    bl_options = {"REGISTER"}
    verb = "Verify"

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def cli_arguments(self) -> list[str]:
        return ["assets", "verify"]

    def finished(self, context, result) -> set[str]:
        # A non-zero exit means the TREE has errors, not that the tool failed.
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


class PARADISE_ASSETS_OT_clean(_CliOperator, Operator):
    """Delete derived output. Keeps .editor/ unless you ask otherwise"""

    bl_idname = "paradise_assets.clean"
    bl_label = "Clean"
    bl_options = {"REGISTER"}
    verb = "Clean"

    #: Off by default: regenerable, but the next open re-imports every GLB and re-renders every
    #: thumbnail.
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

    def cli_arguments(self) -> list[str]:
        return ["assets", "clean"] + ([] if self.editor_too else ["--keep-editor"])

    def finished(self, context, result) -> set[str]:
        if not result.ok:
            return self._report_failure(result)
        self.report({"INFO"}, "Cleaned build/" + (" and .editor/" if self.editor_too else ""))
        return {"FINISHED"}


def status() -> list[tuple[str, str]]:
    """``(icon, message)`` per unready tool. No logging: the panel asks on every redraw."""
    from .host import _preference

    problems: list[tuple[str, str]] = []
    if resolve_cli_command() is None:
        problems.append(("ERROR", "No Paradise CLI — set it in preferences"))
    if resolve_runtime_command() is None:
        problems.append(("ERROR", "No runtime host — set it in preferences"))

    # The pipeline resolves PARADISE_KTX_PATH with File.Exists, so a directory or typo is
    # silently discarded; a field that LOOKS filled in is worse than an empty one.
    ktx = _preference("ktx_path").strip()
    if ktx and not os.path.isfile(os.path.expanduser(ktx)):
        problems.append(("ERROR", "KTX path is not a file — point it at ktx.exe itself"))
    return problems


class PARADISE_ASSETS_OT_build_schema(_CliOperator, Operator):
    """Build the runtime host so it dumps the game's component schema into .editor/"""

    bl_idname = "paradise_assets.build_schema"
    bl_label = "Build Game Schema"
    bl_options = {"REGISTER"}
    verb = "Schema build"

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None and schema_build_stage() is not None

    def execute(self, context):
        # Not the CLI: the schema is a function of the game's C# records and only the game's
        # own launcher build can write it, so this runs `dotnet build` on the configured host.
        found = _project(self)
        if found is None:
            return {"CANCELLED"}
        self._layout, self._document_path = found

        if not _modal_possible(context):
            stage = schema_build_stage()
            if stage is None:
                self.report({"ERROR"}, "Runtime Host is not a csproj; nothing to build")
                return {"CANCELLED"}
            job = start_schema_build(self._layout.root)
            result = None
            while job is not None and result is None:
                time.sleep(POLL_INTERVAL)
                result = job.poll()
            if result is None:
                self.report({"ERROR"}, "Runtime Host is not a csproj; nothing to build")
                return {"CANCELLED"}
            return self.finished(context, result)

        self._job = start_schema_build(self._layout.root)
        if self._job is None:
            self.report({"ERROR"}, "Runtime Host is not a csproj; nothing to build")
            return {"CANCELLED"}

        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(POLL_INTERVAL, window=context.window)
        window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def finished(self, context, result) -> set[str]:
        if not result.ok:
            return self._report_failure(result)
        schema = os.path.join(self._layout.editor, project.SCHEMA_FILE_NAME)
        if not os.path.isfile(schema):
            self.report({"ERROR"}, f"The build succeeded but wrote no {schema}; is ParadiseAuthoringSchemaPath set?")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Schema dumped to {schema}")
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


classes = (
    PARADISE_ASSETS_OT_play,
    PARADISE_ASSETS_OT_build_schema,
    PARADISE_ASSETS_OT_build,
    PARADISE_ASSETS_OT_verify,
    PARADISE_ASSETS_OT_clean,
)
