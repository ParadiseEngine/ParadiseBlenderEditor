"""Build, Play, Stop, Verify and Clean as buttons. Play is ``paradise host play``: the CLI builds
the assets, brings the launcher up to date, runs the game and waits for it, so a failed build
stops the launch (nothing else keeps ``.editor/play/`` fresh) and a Stop is one terminate. The
open document plays; every prefab is playable (§2.9).
"""

from __future__ import annotations

import os
import subprocess
import time

import bpy
from bpy.props import BoolProperty
from bpy.types import Operator

from ..document import project
from ..materialize import store
from . import session
from .host import resolve_cli_command, run_cli, start_cli

__all__ = ["classes"]

# How long Play keeps an eye on the game before handing its lifetime to the player. Long enough
# for a launcher build plus the load: a build that dies a minute in must still be reported here,
# not only in the panel. Blender cannot see whether a window opened; this bounds the failure.
WATCH_SECONDS = 180.0
POLL_INTERVAL = 0.4

#: Background Blender has no event loop; a scripted Play waits this long for an early death.
BACKGROUND_WAIT_SECONDS = 5.0

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


class PARADISE_ASSETS_OT_play(Operator):
    """Build this project and run the game on the open document (replacing a running one)"""

    bl_idname = "paradise_assets.play"
    bl_label = "Build & Play"
    bl_options = {"REGISTER"}

    watch: BoolProperty(  # type: ignore[valid-type]
        name="Watch Code",
        description=(
            "Run under `dotnet watch`: a C# edit is hot-patched into the running game, or "
            "rebuilds and restarts it when it cannot be. Slower to start; no rebuild afterwards"
        ),
        default=False,
    )

    # Plain attributes, not RNA properties: watch state must not reach the redo panel or a keymap.
    _process = None
    _timer = None
    _root = ""
    _deadline = 0.0

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def execute(self, context):
        found = _project(self)
        if found is None:
            return {"CANCELLED"}
        layout, document_path = found

        if resolve_cli_command() is None:
            self.report({"ERROR"}, CLI_MISSING)
            return {"CANCELLED"}

        process, error = session.start(layout.root, document_path, watch=self.watch)
        if process is None:
            self.report({"ERROR"}, error or "Could not start the game")
            return {"CANCELLED"}

        self._process, self._root = process, layout.root
        self.report(
            {"INFO"},
            f"{'Watching and playing' if self.watch else 'Playing'} "
            f"{os.path.basename(document_path)} (pid {process.pid})")

        if not _modal_possible(context):
            return self._background_wait()

        # The panel would show a death too, but only on its next redraw; a build error is worth
        # a report in the author's face. The handler watches the session it started and no other.
        self._deadline = time.monotonic() + WATCH_SECONDS
        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(POLL_INTERVAL, window=context.window)
        window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._process.poll() is None:
            # Still alive past the window: it opened, and its lifetime is the player's business.
            return self._release(context) if time.monotonic() >= self._deadline else {"PASS_THROUGH"}

        status = self._report_exit()
        self._release(context)
        return status

    def cancel(self, context) -> None:
        self._release(context)

    def _background_wait(self) -> set[str]:
        try:
            self._process.wait(timeout=BACKGROUND_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            # Still running, which is the good case.
            return {"FINISHED"}
        return self._report_exit()

    def _report_exit(self) -> set[str]:
        """Reap through the session so the panel and this report agree on the reason."""
        session.process_for(self._root)
        reason = session.exit_reason(self._root)
        if reason is None:
            return {"FINISHED"}
        self.report({"ERROR"}, f"The game stopped — {reason} (see {session.log_path(self._root)})")
        return {"CANCELLED"}

    def _release(self, context) -> set[str]:
        """Drop the timer. Safe to call twice -- cancel also runs on a modal that finished."""
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        return {"FINISHED"}


class PARADISE_ASSETS_OT_stop_play(Operator):
    """Stop the running game (and its dotnet watch, if any)"""

    bl_idname = "paradise_assets.stop_play"
    bl_label = "Stop"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        state = store.read_state(context.scene)
        if state is None:
            return False
        layout = project.locate(state.path)
        return layout is not None and session.is_running(layout.root)

    def execute(self, context):
        found = _project(self)
        if found is None:
            return {"CANCELLED"}
        session.stop(found[0].root)
        self.report({"INFO"}, "Game stopped")
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


#: manifest path -> ((mtime_ns, size), declares a host). The panel asks per redraw.
_HOST_DECLARED: dict[str, tuple[tuple[int, int], bool]] = {}


def declares_host(layout: project.ProjectLayout) -> bool:
    """Whether ``assets/project.toml`` has a ``[host] project``; without one the CLI has nothing
    to run and Play would only say so after a build."""
    path = layout.manifest
    try:
        stat = os.stat(path)
    except OSError:
        return False
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _HOST_DECLARED.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    import tomllib

    try:
        with open(path, "rb") as handle:
            manifest = tomllib.load(handle)
        answer = bool((manifest.get("host") or {}).get("project"))
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        answer = False
    _HOST_DECLARED[path] = (stamp, answer)
    return answer


def status(layout: project.ProjectLayout | None = None) -> list[tuple[str, str]]:
    """``(icon, message)`` per unready tool. No logging: the panel asks on every redraw."""
    from .host import _preference

    problems: list[tuple[str, str]] = []
    if resolve_cli_command() is None:
        problems.append(("ERROR", "No Paradise CLI — set it in preferences"))
    if layout is not None and not declares_host(layout):
        problems.append(("ERROR", "No [host] project in assets/project.toml — nothing to play"))

    # The pipeline resolves PARADISE_KTX_PATH with File.Exists, so a directory or typo is
    # silently discarded; a field that LOOKS filled in is worse than an empty one.
    ktx = _preference("ktx_path").strip()
    if ktx and not os.path.isfile(os.path.expanduser(ktx)):
        problems.append(("ERROR", "KTX path is not a file — point it at ktx.exe itself"))
    return problems


class PARADISE_ASSETS_OT_build_schema(_CliOperator, Operator):
    """Build the game's launcher so it dumps the component schema into .editor/"""

    bl_idname = "paradise_assets.build_schema"
    bl_label = "Build Game Schema"
    bl_options = {"REGISTER"}
    verb = "Schema build"

    @classmethod
    def poll(cls, context) -> bool:
        return store.read_state(context.scene) is not None

    def cli_arguments(self) -> list[str]:
        # The schema is a function of the game's C# records and only the launcher's own build
        # writes it; `host build` is that build, on whatever [host] names.
        return ["host", "build"]

    def finished(self, context, result) -> set[str]:
        if not result.ok:
            return self._report_failure(result)
        schema = os.path.join(self._layout.editor, project.SCHEMA_FILE_NAME)
        if not os.path.isfile(schema):
            self.report(
                {"ERROR"},
                f"The build succeeded but wrote no {schema}; is ParadiseAuthoringSchemaPath set?")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Schema dumped to {schema}")
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


classes = (
    PARADISE_ASSETS_OT_play,
    PARADISE_ASSETS_OT_stop_play,
    PARADISE_ASSETS_OT_build_schema,
    PARADISE_ASSETS_OT_build,
    PARADISE_ASSETS_OT_verify,
    PARADISE_ASSETS_OT_clean,
)
