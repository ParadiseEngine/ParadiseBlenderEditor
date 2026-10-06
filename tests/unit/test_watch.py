"""Tests for the watcher supervisor's bookkeeping.

Spawning a real ``paradise assets watch`` is an integration concern; what is unit-testable here
is the part that goes wrong quietly. **One watcher per project is a correctness rule, not a
tidiness one** -- the engine's ``AssetWatcher.Drain`` documents that its maintainer's quarantine
is unsynchronized and driven outside the gate, so two drainers race it and what they lose is the
identity a move depends on. A second watcher started by accident is a renamed asset getting a
fresh guid and every reference to it dangling.

The process is faked. What is under test is the table, not ``subprocess``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import types
from concurrent.futures import Future

import pytest

from paradise_assets import watch


class FakeProcess:
    """Just enough of ``Popen``: alive until it is told otherwise."""

    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.terminate_calls = 0
        self.wait_timeouts = []
        self.kill_calls = 0
        self.exit_on_terminate = True
        self.signals = []

    def poll(self):
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)
        # Reuse the configurable exit simulation, not an actual OS signal.
        self.terminate()

    def terminate(self):
        self.terminate_calls += 1
        self.terminated = True
        if self.exit_on_terminate:
            self.returncode = 0

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.killed = True
        self.returncode = -9


def _register(root: str) -> FakeProcess:
    """Put a live fake in the table, the way a successful start would."""
    process = FakeProcess()
    watch._WATCHERS[watch._normalize(root)] = process
    return process


@pytest.fixture(autouse=True)
def graceful_fake_watchers(monkeypatch):
    # These fakes model graceful shutdown even when pytest itself runs on Windows.
    monkeypatch.setattr(watch, "_can_terminate_gracefully", lambda: True)
    monkeypatch.setattr(watch, "_uses_job", lambda: False)
    monkeypatch.setattr(watch, "_containment_available", lambda: False)


@pytest.fixture
def launches(tmp_path, monkeypatch):
    """Capture real start/resume arguments without resolving tools or spawning children."""
    from paradise_assets.play import host

    settings = types.SimpleNamespace(
        command=["paradise"], profile="dev", environment={"CAPTURED": "original"},
        calls=[], processes=[], host_calls=[],
    )

    def capture(name, value):
        assert threading.current_thread() is threading.main_thread()
        settings.host_calls.append(name)
        return value

    monkeypatch.setattr(
        host, "resolve_cli_command", lambda root: capture("resolve", settings.command))
    monkeypatch.setattr(host, "ensure_cli_built", lambda root: capture("build", None))
    monkeypatch.setattr(
        host, "_preference", lambda name, default="": capture(name, settings.profile))
    monkeypatch.setattr(
        host, "subprocess_environment", lambda: capture("environment", settings.environment))
    original_log_path = watch.log_path
    monkeypatch.setattr(
        watch, "log_path", lambda root: str(tmp_path / os.path.basename(original_log_path(root))))

    def spawn(argv, **kwargs):
        process = FakeProcess()
        settings.calls.append((argv, kwargs))
        settings.processes.append(process)
        return process

    monkeypatch.setattr(watch.subprocess, "Popen", spawn)
    return settings


@pytest.fixture
def worker():
    """Run real worker-thread APIs; surface assertions with bounded waits, never sleeps."""
    threads = []

    def submit(function):
        result = Future()

        def run():
            try:
                result.set_result(function())
            except BaseException as error:
                result.set_exception(error)

        thread = threading.Thread(target=run, daemon=True)
        threads.append(thread)
        thread.start()
        return result

    yield submit
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), "watch test worker did not finish"


def _reserve(root="/game", *, command=None, profile="dev", environment=None):
    return watch.reserve_pause(
        root, ["paradise"] if command is None else command, profile=profile,
        environment={"CAPTURED": "original"} if environment is None else environment,
    )


def teardown_function(_function):
    watch._WATCHERS.clear()
    # The exit table deliberately OUTLIVES a stopped watcher -- that is the whole point of it, so
    # a panel can say why one died -- which means a test that kills one leaks its reason into the
    # next unless this clears too.
    watch._EXITS.clear()
    watch._PROJECTS.clear()
    watch._OWNERSHIPS.clear()
    watch._LAST_ERRORS.clear()
    watch._REBUILDS.clear()
    watch._ENABLED = True
    watch._HANDLERS = []
    watch._adopting = False


def test_nothing_is_running_to_begin_with():
    assert not watch.is_running("/some/project")
    assert not watch._WATCHERS


def test_a_registered_watcher_reads_as_running():
    _register("/some/project")

    assert watch.is_running("/some/project")
    assert len(watch._WATCHERS) == 1


def test_the_same_project_is_recognised_through_path_spelling():
    # THE case that would silently produce two watchers: the panel and the open operator do not
    # necessarily hand over the same string. A trailing separator, a relative segment or (on
    # Windows) a different case must not look like a different project.
    _register("/some/project")

    assert watch.is_running("/some/project/")
    assert watch.is_running("/some/project/./")
    if os.name == "nt":
        assert watch.is_running("/SOME/PROJECT")


def test_an_exited_watcher_is_reaped_rather_than_remembered():
    # Left in the table, a dead entry makes `start` a no-op forever after the first crash -- the
    # author would see "watching" and get no rebuilds, which is the worst of both.
    process = _register("/some/project")
    process.returncode = 1

    assert not watch.is_running("/some/project")
    assert watch._normalize("/some/project") not in watch._WATCHERS


def test_stop_terminates_and_forgets():
    process = _register("/some/project")

    watch.stop("/some/project")

    assert process.terminated
    assert process.signals == [signal.SIGINT]
    assert not watch.is_running("/some/project")


def test_stop_does_not_force_kill_a_watcher_that_ignores_terminate():
    process = _register("/some/project")
    process.exit_on_terminate = False

    def stubborn(timeout=None):
        process.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(cmd="watch", timeout=timeout)

    process.wait = stubborn
    problem = watch.stop("/some/project")

    assert "Build is blocked" in problem
    assert watch.exit_reason("/some/project") == problem
    assert process.terminate_calls == 1
    assert process.signals == [signal.SIGINT]
    assert process.wait_timeouts == [5.0]
    assert process.kill_calls == 0
    assert watch._WATCHERS[watch._normalize("/some/project")] is process


def test_stop_all_clears_every_project():
    first, second = _register("/project/one"), _register("/project/two")

    watch.stop_all()

    assert first.terminated and second.terminated
    assert not watch._WATCHERS


def test_stopping_something_that_is_not_running_is_harmless():
    watch.stop("/never/started")   # must not raise
    assert not watch._WATCHERS


def test_the_log_path_is_stable_across_processes():
    # THE bug this test exists for, found by driving the thing end to end rather than by reading
    # it. The digest was `hash()`, which Python SALTS PER PROCESS for strings -- so the watcher
    # (started from one Blender session) and the panel (drawing in a later one) computed different
    # filenames, and the panel read a file nothing had ever written. It reported no errors for a
    # watcher that was reporting plenty.
    #
    # Asserted as a literal rather than against a recomputed value: comparing the function to
    # itself passes under a salted hash too, which is exactly how this got through.
    import hashlib
    import os as _os

    root = "/checkout/shiningpie"
    expected = hashlib.sha1(_os.path.normcase(root).encode("utf-8")).hexdigest()[:6]

    assert expected in watch.log_path(root)


def test_last_error_prefers_the_detail_over_the_tally():
    # A failed rebuild ends with "build FAILED with N error(s)", and the line naming the FILE is
    # above it. Showing the tally is showing an author a number they cannot act on.
    import tempfile

    root = tempfile.mkdtemp()
    with open(watch.log_path(root), "w", encoding="utf-8") as handle:
        handle.write(
            "watch: watching /x/assets\n"
            "error: /x/assets/levels/main.prefab: is not valid TOML\n"
            "watch: build FAILED with 1 error(s)\n")

    assert "main.prefab" in (watch.last_error(root) or "")


def test_last_error_falls_back_to_the_tally_when_there_is_no_detail():
    # A failure with nothing above it still has to say something -- silence would read as success.
    import tempfile

    root = tempfile.mkdtemp()
    with open(watch.log_path(root), "w", encoding="utf-8") as handle:
        handle.write("watch: watching /x/assets\nwatch: build FAILED with 2 error(s)\n")

    assert "FAILED" in (watch.last_error(root) or "")


def test_log_paths_differ_per_project():
    # Two checkouts of one game is a normal thing to have; a shared log would interleave them.
    first = watch.log_path("/checkout/one/shiningpie")
    second = watch.log_path("/checkout/two/shiningpie")

    assert first != second
    assert "shiningpie" in os.path.basename(first)


def test_watch_command_rebuilds_play_mode(monkeypatch):
    from paradise_assets.play import host

    monkeypatch.setattr(
        host, "_preference", lambda name, default="": "dev" if name == "build_profile" else default)

    assert watch.watch_command(["paradise"], "/game") == [
        "paradise", "assets", "watch", "--editor", "--profile", "dev", "--project", "/game",
    ]


def test_start_for_does_nothing_when_auto_watch_is_off(monkeypatch):
    # prefs imports bpy, so the module is faked rather than imported: this file is the no-Blender
    # suite, and start_for's `from . import prefs` is the only reason that module is involved.
    class Preferences:
        auto_watch = False

    fake = types.ModuleType("paradise_assets.prefs")
    fake.get_preferences = lambda: Preferences()
    monkeypatch.setitem(sys.modules, "paradise_assets.prefs", fake)
    started: list[str] = []
    monkeypatch.setattr(watch, "start", lambda root: started.append(root) or None)

    assert watch.start_for("/game") is None
    assert started == []


def test_start_for_starts_when_auto_watch_is_on(monkeypatch):
    class Preferences:
        auto_watch = True

    fake = types.ModuleType("paradise_assets.prefs")
    fake.get_preferences = lambda: Preferences()
    monkeypatch.setitem(sys.modules, "paradise_assets.prefs", fake)
    started: list[str] = []
    monkeypatch.setattr(watch, "start", lambda root: started.append(root) or None)

    assert watch.start_for("/game") is None
    assert started == ["/game"]


def test_last_error_reads_the_tail_and_caches_on_the_stamp(tmp_path, monkeypatch):
    # The panel asks per redraw, and the log grows all session: read the tail once per change.
    log = tmp_path / "watch.log"
    monkeypatch.setattr(watch, "log_path", lambda root: str(log))
    log.write_text("x\n" * 100_000 + "error: Models/a.glb is missing\nbuild FAILED with 1 error(s)\n")

    assert watch.last_error("/p") == "error: Models/a.glb is missing"
    calls = []
    monkeypatch.setattr(watch, "_scan_for_error", lambda path: calls.append(path) or "scanned")
    assert watch.last_error("/p") == "error: Models/a.glb is missing"   # cached, not rescanned
    assert calls == []

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("error: another\n")
    assert watch.last_error("/p") == "scanned"
    assert len(calls) == 1


def test_adopt_loaded_file_skips_a_restricted_startup_data(monkeypatch):
    """Blender registers addons against a ``bpy.data`` stub that carries no collections. Reading
    ``scenes`` off it raised straight out of ``register()``, which cost every step after the
    watcher -- the Asset Browser's "Open Prefab Document" entry among them -- and left the classes
    already registered behind, so no later enable could recover without restarting Blender."""

    class RestrictData:
        """``_RestrictData``: it answers to nothing until a file is actually in."""

    fake = types.ModuleType("bpy")
    fake.data = RestrictData()
    monkeypatch.setitem(sys.modules, "bpy", fake)

    # Nothing below the guard may run: this is the no-Blender suite, so reaching the imports
    # under it fails on ``mathutils`` rather than on ``data.scenes``. Either way it is red
    # without the guard, and the Blender-side test reproduces the AttributeError itself.
    watch.adopt_loaded_file()

    assert watch._WATCHERS == {}


def test_last_rebuild_lists_every_diagnostic_of_the_latest_rebuild_only(tmp_path, monkeypatch):
    # The panel shows what the latest rebuild said; an earlier failure that was since fixed must
    # not linger, and the CLI's doubled prefixes and absolute paths read as the author wrote them.
    root = tmp_path / "game"
    log = tmp_path / "watch.log"
    monkeypatch.setattr(watch, "log_path", lambda _root: str(log))
    assets = os.path.join(os.path.realpath(root), "assets") + os.sep
    log.write_text(
        "watch: watching\n"
        "error: old problem\n"
        "watch: build FAILED with 1 error(s)\n"
        f"error: error: {assets}meshes/a.mesh: {assets}meshes/a.mesh: has an unknown key 'asset'\n"
        "  some build chatter\n"
        f"warning: {assets}levels/b.prefab: names nothing\n"
        f"error: {assets}meshes/c.mesh: is missing\n"
        "watch: build FAILED with 2 error(s)\n")

    rebuild = watch.last_rebuild(str(root))

    assert rebuild.failed and rebuild.summary == "build FAILED with 2 error(s)"
    assert rebuild.diagnostics == (
        ("error", "meshes/a.mesh: has an unknown key 'asset'"),
        ("warning", "levels/b.prefab: names nothing"),
        ("error", "meshes/c.mesh: is missing"),
    )


def test_a_successful_rebuild_clears_the_list(tmp_path, monkeypatch):
    log = tmp_path / "watch.log"
    monkeypatch.setattr(watch, "log_path", lambda _root: str(log))
    log.write_text("error: x\nwatch: build FAILED with 1 error(s)\nwatch: rebuilt 12 asset(s) into build\n")

    rebuild = watch.last_rebuild(str(tmp_path))

    assert not rebuild.failed and rebuild.diagnostics == ()


def test_no_rebuild_yet_reads_as_none(tmp_path, monkeypatch):
    log = tmp_path / "watch.log"
    monkeypatch.setattr(watch, "log_path", lambda _root: str(log))
    log.write_text("watch: watching /x/assets\n")

    assert watch.last_rebuild(str(tmp_path)) is None


def test_explicit_profile_makes_watch_command_worker_safe(monkeypatch, worker):
    from paradise_assets.play import host

    def unexpected_preference(*args):
        pytest.fail("an explicit profile must not read preferences")

    monkeypatch.setattr(host, "_preference", unexpected_preference)
    command = ["dotnet", "paradise.dll"]

    assert worker(lambda: watch.watch_command(command, "/game", profile="release")).result(5) == [
        "dotnet", "paradise.dll", "assets", "watch", "--editor",
        "--profile", "release", "--project", "/game",
    ]
    assert command == ["dotnet", "paradise.dll"]


@pytest.mark.parametrize("api", ["prepare_pause", "reserve_pause", "watch_command"])
def test_settings_must_be_captured_on_the_main_thread(api, launches, worker):
    calls = {
        "prepare_pause": lambda: watch.prepare_pause("/game", ["paradise"], {}),
        "reserve_pause": lambda: _reserve(),
        "watch_command": lambda: watch.watch_command(["paradise"], "/game"),
    }

    with pytest.raises(RuntimeError, match="main thread"):
        worker(calls[api]).result(5)

    assert launches.host_calls == []
    assert launches.calls == []
    assert not watch.is_paused("/game")


@pytest.mark.parametrize("api", ["start", "start_for"])
def test_worker_cannot_request_a_fresh_start(api, launches, worker):
    problem = worker(lambda: getattr(watch, api)("/game")).result(5)

    assert "main thread" in problem
    assert launches.host_calls == []
    assert launches.calls == []


@pytest.mark.parametrize("profile, expected", [("release", "release"), ("", "dev")])
def test_prepare_captures_settings_and_worker_restores_without_host_access(
    profile, expected, launches, worker,
):
    original = _register("/game")
    command = ["dotnet", "captured.dll"]
    environment = {"SDK": "captured"}
    launches.profile = profile

    token = watch.prepare_pause("/game/./", command, environment)

    assert launches.host_calls == ["build_profile"]
    assert token.root == watch._normalize("/game")
    assert watch.is_paused("/game")
    assert watch.is_running("/game")  # Reserving does not stop a writer on the UI thread.
    assert original.terminate_calls == 0
    command[:] = ["wrong-cli"]
    environment["SDK"] = "changed"
    launches.profile = "changed"

    def build():
        assert token.pause() is None
        assert not watch.is_running("/game")
        assert token.resume() is None
        assert token.resume() is None

    worker(build).result(5)

    assert launches.host_calls == ["build_profile"]
    assert len(launches.calls) == 1
    argv, options = launches.calls[0]
    assert argv == [
        "dotnet", "captured.dll", "assets", "watch", "--editor",
        "--profile", expected, "--project", token.root,
    ]
    assert options["cwd"] == token.root
    assert options["env"] == {"SDK": "captured"}
    assert options["stdout"].closed
    assert options["stderr"] == subprocess.STDOUT
    assert options["stdin"] == subprocess.DEVNULL
    assert original.terminate_calls == 1
    assert original.signals == [signal.SIGINT]
    assert original.wait_timeouts == [5.0]
    assert original.kill_calls == 0
    assert watch.is_running("/game")
    assert not watch.is_paused("/game")
    assert "already been released" in token.pause()


def test_duplicate_starts_resolve_and_spawn_only_once(launches):
    assert watch.start("/game") is None
    assert watch.start("/game/./") is None
    assert watch.start("/game/") is None
    assert watch.ensure("/game") is None

    assert len(launches.calls) == 1
    assert launches.host_calls == ["resolve", "build", "build_profile", "environment"]


def test_reentrant_host_resolution_cannot_start_a_second_watcher(launches, monkeypatch):
    from paradise_assets.play import host

    nested = []

    def resolve(root):
        nested.append(watch.start(root))
        return ["paradise"]

    monkeypatch.setattr(host, "resolve_cli_command", resolve)

    assert watch.start("/game") is None
    assert len(nested) == 1
    assert "already being prepared" in nested[0]
    assert len(launches.calls) == 1
    assert not watch._PROJECTS[watch._normalize("/game")].starting


@pytest.mark.parametrize("action", ["stop", "stop_all", "unregister_handler"])
def test_stop_during_reentrant_host_resolution_cancels_start(action, launches, monkeypatch):
    from paradise_assets.play import host

    def resolve(root):
        if action == "stop":
            watch.stop(root)
        else:
            getattr(watch, action)()
        return ["paradise"]

    monkeypatch.setattr(host, "resolve_cli_command", resolve)

    assert "cancelled by Stop" in watch.start("/game")
    assert launches.calls == []
    state = watch._PROJECTS[watch._normalize("/game")]
    assert not state.starting
    assert state.pending is None


@pytest.mark.parametrize("failure", ["missing-cli", "build-error", "exception"])
def test_failed_start_releases_the_preparation_guard(failure, launches, monkeypatch):
    from paradise_assets.play import host

    with monkeypatch.context() as patch:
        if failure == "missing-cli":
            patch.setattr(host, "resolve_cli_command", lambda root: None)
            assert "No `paradise` CLI" in watch.start("/game")
        elif failure == "build-error":
            patch.setattr(host, "ensure_cli_built", lambda root: "CLI build failed")
            assert watch.start("/game") == "CLI build failed"
        else:
            def broken_resolution(root):
                raise RuntimeError("resolution failed")

            patch.setattr(host, "resolve_cli_command", broken_resolution)
            with pytest.raises(RuntimeError, match="resolution failed"):
                watch.start("/game")

    assert launches.calls == []
    assert not watch._PROJECTS[watch._normalize("/game")].starting
    assert watch.start("/game") is None
    assert len(launches.calls) == 1


def test_overlapping_reservations_serialize_builds_and_restore_only_after_last_release(
    launches, worker,
):
    original = _register("/game")
    first, second = _reserve(), _reserve("/game/./")
    building, release = threading.Event(), threading.Event()

    def first_build():
        try:
            assert first.pause() is None
            assert first.pause() is None  # The owner may check twice without another shutdown.
            building.set()
            assert release.wait(5), "first build was not released"
        finally:
            assert first.resume() is None

    running = worker(first_build)
    try:
        assert building.wait(5), "first build did not acquire the pause"
        problem = worker(second.pause).result(5)
        assert "Another build owns" in problem
        assert launches.calls == []
    finally:
        release.set()
    running.result(5)

    assert watch.is_paused("/game")
    assert not watch.is_running("/game")
    assert launches.calls == []
    assert worker(second.pause).result(5) is None
    assert worker(second.resume).result(5) is None
    assert not watch.is_paused("/game")
    assert len(launches.calls) == 1
    assert original.terminate_calls == 1


def test_releasing_a_nonowner_does_not_release_the_build_slot(launches):
    _register("/game")
    owner, cancelled, later = _reserve(), _reserve(), _reserve()

    assert owner.pause() is None
    assert cancelled.resume() is None
    assert "Another build owns" in later.pause()
    assert launches.calls == []
    assert owner.resume() is None
    assert launches.calls == []
    assert later.pause() is None
    assert later.resume() is None
    assert len(launches.calls) == 1


def test_different_projects_can_pause_and_restore_independently(launches, worker):
    first, second = _register("/one"), _register("/two")
    one, two = _reserve("/one"), _reserve("/two")

    assert worker(one.pause).result(5) is None
    assert worker(two.pause).result(5) is None
    assert first.terminated and second.terminated
    assert worker(one.resume).result(5) is None
    assert watch.is_running("/one")
    assert not watch.is_paused("/one")
    assert watch.is_paused("/two")
    assert not watch.is_running("/two")
    assert len(launches.calls) == 1
    assert worker(two.resume).result(5) is None
    assert watch.is_running("/two")
    assert len(launches.calls) == 2


def test_slow_stop_does_not_block_another_projects_start_or_stop(launches, worker):
    slow = _register("/slow")
    slow.exit_on_terminate = False
    waiting, release = threading.Event(), threading.Event()

    def wait_for_exit(timeout=None):
        slow.wait_timeouts.append(timeout)
        waiting.set()
        assert release.wait(5), "another project's operations blocked behind this stop"
        slow.returncode = 0
        return 0

    slow.wait = wait_for_exit
    stopping = worker(lambda: watch.stop("/slow"))
    try:
        assert waiting.wait(5), "slow stop did not reach its shutdown wait"
        assert watch.start("/other") is None
        assert watch.is_running("/other")
        assert watch.stop("/other") is None
        assert launches.processes[0].terminated
    finally:
        release.set()
    assert stopping.result(5) is None
    assert slow.wait_timeouts == [5.0]
    assert not watch.is_running("/slow")


def test_latest_reservation_supplies_a_copied_restoration_snapshot(launches):
    _register("/game")
    first = _reserve(command=["old-cli"], profile="old", environment={"SDK": "old"})
    assert first.pause() is None
    command, environment = ["new-cli"], {"SDK": "new"}
    latest = _reserve(command=command, profile="new", environment=environment)
    command.append("mutated")
    environment["SDK"] = "mutated"

    assert latest.resume() is None
    assert launches.calls == []
    assert first.resume() is None

    assert len(launches.calls) == 1
    argv, options = launches.calls[0]
    assert argv == [
        "new-cli", "assets", "watch", "--editor", "--profile", "new",
        "--project", watch._normalize("/game"),
    ]
    assert options["env"] == {"SDK": "new"}


@pytest.mark.parametrize("initial", ["inactive", "manually-stopped", "running"])
def test_explicit_start_is_deferred_and_its_latest_settings_win(initial, launches):
    if initial != "inactive":
        _register("/game")
    if initial == "manually-stopped":
        assert watch.stop("/game") is None
    token = _reserve(command=["reservation-cli"], profile="reservation")
    assert token.pause() is None
    assert watch.start("/game") is None
    launches.command = ["new-cli"]
    launches.profile = "release"
    launches.environment = {"SDK": "new"}
    assert watch.start("/game") is None
    launches.command.append("too-late")
    launches.environment["SDK"] = "too-late"
    launches.profile = "too-late"

    assert launches.calls == []
    assert launches.host_calls == ["resolve", "build_profile", "environment"] * 2
    assert not watch.is_running("/game")
    assert token.resume() is None
    assert token.resume() is None
    assert len(launches.calls) == 1
    argv, options = launches.calls[0]
    assert argv == [
        "new-cli", "assets", "watch", "--editor", "--profile", "release",
        "--project", watch._normalize("/game"),
    ]
    assert options["env"] == {"SDK": "new"}
    assert watch.is_running("/game")
    assert "build" not in launches.host_calls


@pytest.mark.parametrize("shutdown", [False, True])
def test_ensure_never_claims_readiness_during_a_reservation(shutdown, launches):
    _register("/game")
    token = _reserve()
    if shutdown:
        assert token.pause() is None
    pending = watch._PROJECTS[token.root].pending
    launches.profile = "must-not-replace-the-reservation"

    problem = watch.ensure("/game")

    assert "paused for Build & Play" in problem
    assert "nothing can be created" in problem
    assert launches.calls == []
    assert launches.host_calls == []
    assert watch._PROJECTS[token.root].pending is pending
    assert watch.is_running("/game") is (not shutdown)
    assert token.resume() is None
    assert watch.ensure("/game") is None


@pytest.mark.parametrize("initial", ["inactive", "manually-stopped"])
def test_reservation_does_not_enable_an_inactive_or_manually_disabled_watcher(initial, launches):
    if initial == "manually-stopped":
        original = _register("/game")
        assert watch.stop("/game") is None
        assert original.terminated
    token = _reserve()

    assert watch.is_paused("/game")
    assert "paused" in watch.ensure("/game")
    assert watch._PROJECTS[token.root].pending is None
    assert token.pause() is None
    assert "paused" in watch.ensure("/game")
    assert watch._PROJECTS[token.root].pending is None
    assert token.resume() is None
    assert token.resume() is None
    assert not watch.is_paused("/game")
    assert not watch.is_running("/game")
    assert launches.calls == []
    assert launches.host_calls == []


def test_cancelled_preparation_releases_without_stopping_the_existing_watcher(launches, worker):
    original = _register("/game")
    token = _reserve()

    assert worker(token.resume).result(5) is None
    assert worker(token.resume).result(5) is None
    assert "already been released" in worker(token.pause).result(5)
    assert not watch.is_paused("/game")
    assert watch._WATCHERS[watch._normalize("/game")] is original
    assert original.terminate_calls == 0
    assert launches.calls == []


@pytest.mark.parametrize("action", ["stop", "stop_all", "unregister_handler"])
@pytest.mark.parametrize("paused", [False, True])
def test_stop_suppresses_all_pending_restoration(action, paused, launches, worker):
    original = _register("/game")
    first, second = _reserve(), _reserve()
    assert watch.start("/game") is None  # An explicit deferred start must also be suppressed.
    if paused:
        assert worker(first.pause).result(5) is None

    if action == "stop":
        assert watch.stop("/game") is None
    else:
        assert getattr(watch, action)() is None
        assert "cancel" in worker(second.pause).result(5).lower()
        if action == "unregister_handler":
            assert "stopped" in watch.start("/new-project")

    assert worker(first.resume).result(5) is None
    assert worker(second.resume).result(5) is None
    assert not watch.is_paused("/game")
    assert not watch.is_running("/game")
    assert original.terminate_calls == 1
    assert launches.calls == []


def test_stop_all_cancels_old_reservations_but_allows_later_start_and_pause(launches, worker):
    old = _reserve("/game")
    assert watch.start("/game") is None

    assert watch.stop_all() is None
    assert "cancelled" in worker(old.pause).result(5)
    assert worker(old.resume).result(5) is None
    assert launches.calls == []
    assert watch.start("/game") is None
    fresh = _reserve("/game")
    assert worker(fresh.pause).result(5) is None
    assert worker(fresh.resume).result(5) is None
    assert len(launches.calls) == 2
    assert not watch.is_paused("/game")
    assert watch.is_running("/game")


def test_registration_reenables_starts_but_not_old_cancelled_tokens(launches, monkeypatch):
    handlers = types.SimpleNamespace(load_post=[], persistent=lambda function: function)
    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(handlers=handlers)
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    adopted = []
    monkeypatch.setattr(watch, "adopt_loaded_file", lambda: adopted.append(True))
    _register("/game")
    old = _reserve()
    assert old.pause() is None

    watch.register_handler()
    watch.register_handler()
    assert handlers.load_post == [watch._on_load_post]
    assert adopted == [True, True]
    watch.unregister_handler()
    assert handlers.load_post == []
    assert not watch._ENABLED
    watch.unregister_handler()  # Disabling twice is harmless.
    assert "stopped" in watch.start("/game")
    disabled = _reserve("/new-project")
    assert "stopped" in disabled.pause()
    assert disabled.resume() is None

    watch.register_handler()
    assert watch._ENABLED
    assert "cancelled" in old.pause()
    assert old.resume() is None
    assert launches.calls == []
    assert watch.start("/game") is None
    assert len(launches.calls) == 1


@pytest.mark.parametrize("outcome", ["failed-build", "exception", "system-exit"])
def test_build_worker_finally_restores_watcher_after_failure(
    outcome, launches, monkeypatch, tmp_path,
):
    from paradise_assets.play import session

    original = _register("/game")
    token = _reserve()
    build = FakeProcess()
    build.returncode = 7
    events = []
    spawn_watcher = watch.subprocess.Popen

    def spawn(argv, **options):
        if argv == ["fake-build"]:
            assert not watch.is_running("/game")
            assert watch.is_paused("/game")
            events.append("build")
            return build
        events.append("restore")
        return spawn_watcher(argv, **options)

    def monitor(self, reader):
        if outcome == "exception":
            raise RuntimeError("build exploded")
        if outcome == "system-exit":
            raise SystemExit("build exploded")
        return 7

    def release(root, ownership):
        events.append("cleanup")
        return None

    monkeypatch.setattr(session.subprocess, "Popen", spawn)
    monkeypatch.setattr(session, "log_path", lambda root: str(tmp_path / "play.log"))
    monkeypatch.setattr(session.PlaySession, "_monitor", monitor)
    monkeypatch.setattr(session.process_tree, "launch_options", lambda environment: {"env": environment})
    monkeypatch.setattr(session.process_tree, "record", lambda *args, **kwargs: object())
    monkeypatch.setattr(session.process_tree, "release", release)

    process = session.PlaySession(
        "/game", [("Build", ["fake-build"]), ("Play", ["must-not-play"])],
        {}, watch=False, asset_pause=token,
    )
    assert process.wait(timeout=5) == (7 if outcome == "failed-build" else 1)
    process._thread.join(timeout=5)
    assert not process._thread.is_alive()
    if outcome != "failed-build":
        assert "build exploded" in process.detail
    assert events == ["build", "cleanup", "restore"]
    assert build.wait_timeouts == [2.0]
    assert original.terminate_calls == 1
    assert not watch.is_paused("/game")
    assert watch.is_running("/game")
    assert token.resume() is None
    assert len(launches.calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "terminate-error", "wait-error", "unconfirmed-exit"])
def test_pause_blocks_build_until_exit_is_confirmed_without_forcing_a_kill(failure, launches):
    original = _register("/game")
    original.exit_on_terminate = False
    token = _reserve()

    def terminate():
        original.terminate_calls += 1
        if failure == "terminate-error":
            raise OSError("termination denied")

    def wait(timeout=None):
        original.wait_timeouts.append(timeout)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("watch", timeout)
        if failure == "wait-error":
            raise subprocess.SubprocessError("wait failed")
        return 0  # A wait result alone is not proof: poll still says it is running.

    original.terminate = terminate
    original.wait = wait

    problem = token.pause()

    assert "Build is blocked" in problem
    assert watch.exit_reason("/game") == problem
    assert watch._WATCHERS[token.root] is original
    assert watch._PROJECTS[token.root].owner is None
    assert original.terminate_calls == 1
    assert original.wait_timeouts == ([] if failure == "terminate-error" else [5.0])
    assert original.kill_calls == 0
    assert launches.calls == []

    original.returncode = 0  # The user has now closed the watcher and its children.
    assert token.pause() is None
    assert original.terminate_calls == 1
    assert not watch.is_running("/game")
    assert token.resume() is None
    assert watch.exit_reason("/game") is None
    assert len(launches.calls) == 1


def test_timeout_with_a_late_exit_still_requires_a_confirming_retry(launches):
    original = _register("/game")
    original.exit_on_terminate = False
    token = _reserve()

    def timed_out(timeout=None):
        original.returncode = 0
        raise subprocess.TimeoutExpired("watch", timeout)

    original.wait = timed_out

    assert "Build is blocked" in token.pause()
    assert watch._WATCHERS[token.root] is original
    assert watch._PROJECTS[token.root].owner is None
    assert token.pause() is None
    assert token.resume() is None
    assert original.kill_calls == 0
    assert len(launches.calls) == 1


@pytest.mark.parametrize("reap_first", [False, True])
@pytest.mark.parametrize("exit_code", [1, -9])
def test_abnormal_uncontained_exit_blocks_build_and_restoration(reap_first, exit_code, launches):
    original = _register("/game")
    token = _reserve()
    original.returncode = exit_code
    if reap_first:
        assert not watch.is_running("/game")

    problem = token.pause()

    assert "child writers may remain" in problem
    assert watch.exit_reason("/game") == problem
    assert token.resume() == problem
    assert token.resume() is None
    assert watch.start("/game") == problem
    assert not watch.is_paused("/game")
    assert launches.calls == []
    assert original.terminate_calls == original.kill_calls == 0


def test_uncontained_windows_watcher_fails_closed(launches, windows_jobs):
    original = _register("/game")
    token = _reserve()

    problem = token.pause()

    assert "no safe graceful termination" in problem
    assert "Build is blocked" in problem
    assert watch._WATCHERS[token.root] is original
    assert watch._PROJECTS[token.root].owner is None
    assert original.terminate_calls == original.kill_calls == 0
    assert original.wait_timeouts == []
    assert watch.stop("/game") == problem
    assert token.resume() is None
    assert launches.calls == []
    assert windows_jobs.stops == windows_jobs.records == windows_jobs.releases == []


def test_stop_all_reports_each_failed_stop_without_losing_the_processes(launches, monkeypatch):
    first, second = _register("/one"), _register("/two")
    one, two = _reserve("/one"), _reserve("/two")
    monkeypatch.setattr(watch, "_can_terminate_gracefully", lambda: False)

    problem = watch.stop_all()

    assert watch._normalize("/one") in problem
    assert watch._normalize("/two") in problem
    assert watch._WATCHERS[one.root] is first
    assert watch._WATCHERS[two.root] is second
    assert first.kill_calls == second.kill_calls == 0
    assert "cancelled" in one.pause()
    assert "cancelled" in two.pause()
    assert one.resume() is None
    assert two.resume() is None
    assert launches.calls == []


@pytest.mark.parametrize("failure", ["log", "spawn-os-error", "spawn-subprocess-error"])
def test_failed_resume_releases_reservation_closes_log_and_does_not_retry(
    failure, launches, monkeypatch, worker,
):
    _register("/game")
    token = _reserve()
    assert token.pause() is None
    attempts, handles = [], []

    def cannot_open(*args, **kwargs):
        attempts.append("log")
        raise OSError("log denied")

    def cannot_spawn(*args, **kwargs):
        attempts.append("spawn")
        handles.append(kwargs["stdout"])
        error = OSError if failure == "spawn-os-error" else subprocess.SubprocessError
        raise error("spawn denied")

    with monkeypatch.context() as patch:
        if failure == "log":
            patch.setattr(watch, "open", cannot_open, raising=False)
        else:
            patch.setattr(watch.subprocess, "Popen", cannot_spawn)
        problem = worker(token.resume).result(5)
        assert ("Could not open the watch log" if failure == "log" else "Could not start") in problem
        assert watch.exit_reason("/game") == problem
        assert worker(token.resume).result(5) is None
        assert "already been released" in token.pause()

    assert attempts == ["log" if failure == "log" else "spawn"]
    assert all(handle.closed for handle in handles)
    assert not watch.is_paused("/game")
    assert not watch.is_running("/game")
    state = watch._PROJECTS[token.root]
    assert state.owner is None
    assert state.pending is None
    assert launches.calls == []
    assert watch.start("/game") is None  # Recovery requires a new explicit request.
    assert watch.exit_reason("/game") is None
    assert len(launches.calls) == 1


def test_ensure_rejects_a_child_that_exits_immediately(launches, monkeypatch):
    def exited(*args, **kwargs):
        process = FakeProcess()
        process.returncode = 1
        return process

    monkeypatch.setattr(watch.subprocess, "Popen", exited)

    assert "exited before it could mint an identity" in watch.ensure("/game")
    assert not watch.is_running("/game")


@pytest.fixture
def windows_jobs(launches, monkeypatch):
    """Exercise the Windows branch with no native Job, process, or ownership-file calls."""
    jobs = types.SimpleNamespace(
        options={"env": {"CAPTURED": "original", "OWNER": "marker"}, "creationflags": 0x08000004},
        environments=[], records=[], releases=[], stops=[],
    )
    monkeypatch.setattr(watch, "_uses_job", lambda: True)
    monkeypatch.setattr(watch, "_can_terminate_gracefully", lambda: False)
    monkeypatch.setattr(watch, "_containment_available", lambda: True)

    def launch_options(environment):
        jobs.environments.append(environment)
        return jobs.options

    def record(root, process, *, environment):
        owner = watch.process_tree.Ownership(root, f"owner-{len(jobs.records)}")
        jobs.records.append((root, process, environment, owner))
        return owner

    def release(root, ownership):
        jobs.releases.append((root, ownership))
        for _, process, _, owner in jobs.records:
            if owner == ownership:
                process.returncode = 0
        return None

    def stop(root):
        jobs.stops.append(root)
        return None

    monkeypatch.setattr(watch.process_tree, "launch_options", launch_options)
    monkeypatch.setattr(watch.process_tree, "record", record)
    monkeypatch.setattr(watch.process_tree, "release", release)
    monkeypatch.setattr(watch.process_tree, "stop", stop)
    return jobs


def test_windows_spawn_forwards_job_options_and_uses_a_separate_ownership_key(launches, windows_jobs):
    assert watch.start("/game") is None

    root = watch._normalize("/game")
    ownership_root = os.path.join(root, ".editor", "asset-watcher")
    assert watch._ownership_root(root) == ownership_root
    assert windows_jobs.stops == [ownership_root]
    assert windows_jobs.environments == [{"CAPTURED": "original"}]
    assert len(launches.calls) == len(windows_jobs.records) == 1
    _, options = launches.calls[0]
    assert options["env"] is windows_jobs.options["env"]
    assert options["creationflags"] == windows_jobs.options["creationflags"]
    assert "start_new_session" not in options
    assert options["cwd"] == root
    assert options["stdout"].closed
    recorded_root, process, environment, owner = windows_jobs.records[0]
    assert recorded_root == ownership_root
    assert environment is options["env"]
    assert process is launches.processes[0]
    assert watch._OWNERSHIPS[root] is owner


def test_windows_pause_releases_the_job_before_permitting_build_and_restores_once(
    launches, windows_jobs, worker,
):
    assert watch.start("/game") is None
    original = launches.processes[0]
    token = _reserve()
    owner = watch._OWNERSHIPS[token.root]

    assert worker(token.pause).result(5) is None

    assert windows_jobs.releases == [(os.path.join(token.root, ".editor", "asset-watcher"), owner)]
    assert original.wait_timeouts == [5.0]
    assert original.terminate_calls == original.kill_calls == 0
    assert token.root not in watch._WATCHERS
    assert token.root not in watch._OWNERSHIPS
    assert watch._PROJECTS[token.root].owner is token
    assert worker(token.resume).result(5) is None
    assert worker(token.resume).result(5) is None
    assert len(launches.calls) == len(windows_jobs.records) == 2
    assert watch._OWNERSHIPS[token.root] is not owner
    assert watch.is_running("/game")


@pytest.mark.parametrize("leader_exited", [False, True])
def test_windows_release_failure_retains_ownership_and_blocks_build(
    leader_exited, launches, windows_jobs, monkeypatch,
):
    assert watch.start("/game") is None
    original = launches.processes[0]
    token = _reserve()
    owner = watch._OWNERSHIPS[token.root]
    if leader_exited:
        original.returncode = 0

    with monkeypatch.context() as patch:
        patch.setattr(watch.process_tree, "release", lambda root, ownership: "Job still has writers")
        problem = token.pause()
        assert "Job still has writers" in problem
        assert "Build is blocked" in problem
        assert watch.exit_reason("/game") == problem
        assert watch._WATCHERS[token.root] is original
        assert watch._OWNERSHIPS[token.root] is owner
        assert watch._PROJECTS[token.root].owner is None
        assert original.wait_timeouts == []
        assert original.kill_calls == original.terminate_calls == 0
        assert len(launches.calls) == 1
        if leader_exited:
            assert not watch.is_running("/game")
            assert watch._OWNERSHIPS[token.root] is owner
            assert watch.exit_reason("/game") == problem
            assert watch._PROJECTS[token.root].error == problem

    assert token.pause() is None
    assert token.root not in watch._OWNERSHIPS
    assert watch.exit_reason("/game") is None
    assert token.resume() is None
    assert len(launches.calls) == 2


@pytest.mark.parametrize("failure", ["timeout", "wait-error", "still-running"])
def test_windows_job_release_also_requires_confirmed_leader_exit(
    failure, launches, windows_jobs, monkeypatch,
):
    assert watch.start("/game") is None
    original = launches.processes[0]
    token = _reserve()
    owner = watch._OWNERSHIPS[token.root]

    def wait(timeout=None):
        original.wait_timeouts.append(timeout)
        if failure == "timeout":
            raise subprocess.TimeoutExpired("watch", timeout)
        if failure == "wait-error":
            raise OSError("wait denied")
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(watch.process_tree, "release", lambda root, ownership: None)
        patch.setattr(original, "wait", wait)
        problem = token.pause()
        assert "Build is blocked" in problem
        assert watch._OWNERSHIPS[token.root] is owner
        assert watch._WATCHERS[token.root] is original
        assert original.wait_timeouts == [5.0]
        assert original.kill_calls == original.terminate_calls == 0
        assert watch._PROJECTS[token.root].owner is None

    assert token.pause() is None
    assert token.resume() is None
    assert len(launches.calls) == 2


def test_windows_reaped_leader_does_not_discard_descendant_ownership(
    launches, windows_jobs, monkeypatch,
):
    assert watch.start("/game") is None
    token = _reserve()
    owner = watch._OWNERSHIPS[token.root]
    launches.processes[0].returncode = -9
    assert not watch.is_running("/game")
    assert token.root not in watch._WATCHERS
    assert watch._OWNERSHIPS[token.root] is owner

    with monkeypatch.context() as patch:
        patch.setattr(watch.process_tree, "release", lambda root, ownership: "descendants remain")
        assert "descendants remain" in token.pause()
        assert watch._OWNERSHIPS[token.root] is owner

    assert token.pause() is None
    assert token.root not in watch._OWNERSHIPS
    assert token.resume() is None
    assert watch.is_running("/game")
    assert len(launches.calls) == 2


@pytest.mark.parametrize("action", ["start", "pause", "stop"])
def test_windows_reclaims_persisted_owner_before_start_or_build(
    action, launches, windows_jobs, monkeypatch,
):
    root = watch._normalize("/game")
    calls = []

    def cannot_reclaim(key):
        calls.append(key)
        return "persisted Job cannot be reclaimed"

    token = _reserve()
    with monkeypatch.context() as patch:
        patch.setattr(watch.process_tree, "stop", cannot_reclaim)
        if action == "start":
            assert token.resume() is None
            problem = watch.start(root)
        elif action == "pause":
            problem = token.pause()
        else:
            problem = watch.stop(root)
        assert "persisted Job cannot be reclaimed" in problem
        assert calls == [os.path.join(root, ".editor", "asset-watcher")]
        assert launches.calls == []
        assert windows_jobs.records == []

    if action == "start":
        assert watch.start(root) is None
    elif action == "pause":
        assert token.pause() is None
        assert token.resume() is None
    else:
        assert watch.stop(root) is None
        assert token.resume() is None
    assert watch.exit_reason(root) is None


def test_windows_stop_all_reclaims_ownership_even_without_a_process_entry(windows_jobs):
    root = watch._normalize("/old-game")
    ownership_root = os.path.join(root, ".editor", "asset-watcher")
    owner = watch.process_tree.Ownership(ownership_root, "old-owner")
    watch._OWNERSHIPS[root] = owner

    assert watch.stop_all() is None

    assert windows_jobs.releases == [(ownership_root, owner)]
    assert not watch._OWNERSHIPS
    assert not watch._WATCHERS


@pytest.mark.parametrize("cleanup", ["confirmed", "kill-error", "wait-timeout", "record-remains"])
def test_windows_failed_record_cleans_only_the_suspended_child_or_retains_a_blocker(
    cleanup, launches, windows_jobs, monkeypatch,
):
    def fail_record(root, process, *, environment):
        raise OSError("cannot record Job")

    def kill_error(self):
        self.kill_calls += 1
        raise OSError("kill denied")

    def wait_timeout(self, timeout=None):
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired("suspended watch", timeout)

    calls = []

    def reclaim(root):
        calls.append(root)
        if cleanup == "record-remains" and len(calls) > 1:
            return "record cleanup denied"
        return None

    monkeypatch.setattr(watch.process_tree, "record", fail_record)
    monkeypatch.setattr(watch.process_tree, "stop", reclaim)
    if cleanup == "kill-error":
        monkeypatch.setattr(FakeProcess, "kill", kill_error)
    elif cleanup == "wait-timeout":
        monkeypatch.setattr(FakeProcess, "wait", wait_timeout)

    problem = watch.start("/game")

    assert "cannot record Job" in problem
    root = watch._normalize("/game")
    ownership_root = os.path.join(root, ".editor", "asset-watcher")
    assert calls == [ownership_root, ownership_root]
    original = launches.processes[0]
    assert original.kill_calls == 1
    assert original.terminate_calls == 0
    assert original.wait_timeouts == ([] if cleanup == "kill-error" else [5.0])
    assert launches.calls[0][1]["stdout"].closed
    assert root not in watch._OWNERSHIPS
    assert watch.exit_reason(root) == problem
    if cleanup == "confirmed":
        assert root not in watch._WATCHERS
        assert watch._PROJECTS[root].unsafe is None
    else:
        assert "cannot confirm cleanup" in problem
        assert watch._WATCHERS[root] is original
        token = _reserve()
        blocked = token.pause()
        assert "restart Blender" in blocked
        if cleanup == "kill-error":
            assert token.resume() == problem
        else:
            assert token.resume() is None
        assert watch.start(root) == blocked
        assert len(launches.calls) == 1


def test_paused_status_and_ensure_do_not_wait_for_shutdown(launches, worker):
    original = _register("/game")
    original.exit_on_terminate = False
    token = _reserve()
    waiting, release = threading.Event(), threading.Event()

    def wait_for_exit(timeout=None):
        original.wait_timeouts.append(timeout)
        waiting.set()
        assert release.wait(5), "a paused status query blocked behind watcher shutdown"
        original.returncode = 0
        return 0

    original.wait = wait_for_exit
    pausing = worker(token.pause)
    try:
        assert waiting.wait(5), "pause did not reach its shutdown wait"
        assert watch.is_paused("/game")
        assert "paused" in watch.ensure("/game")
        assert launches.host_calls == []
    finally:
        release.set()
    assert pausing.result(5) is None
    assert token.resume() is None
    assert not watch.is_paused("/game")
    assert len(launches.calls) == 1


def test_reaping_a_clean_uncontained_exit_clears_the_previous_stop_error(launches):
    original = _register("/game")
    original.exit_on_terminate = False
    problem = watch.stop("/game")
    assert "Build is blocked" in problem
    assert watch.exit_reason("/game") == problem

    original.returncode = 0
    assert not watch.is_running("/game")

    root = watch._normalize("/game")
    assert watch._PROJECTS[root].error is None
    assert watch._PROJECTS[root].unsafe is None
    assert watch._EXITS[root][0] == 0
    assert "Build is blocked" not in (watch.exit_reason("/game") or "")
    token = _reserve()
    assert token.pause() is None
    assert token.resume() is None
    assert launches.calls == []  # Reaping a stopped watcher does not re-enable it.


@pytest.fixture
def posix_ownership(windows_jobs, monkeypatch):
    """Use the same ownership fakes, but never run native POSIX containment code."""
    monkeypatch.setattr(watch, "_uses_job", lambda: False)
    monkeypatch.setattr(watch, "_can_terminate_gracefully", lambda: True)
    windows_jobs.options = {
        "env": {"CAPTURED": "original", "OWNER": "posix-marker"}, "start_new_session": True,
    }
    return windows_jobs


def test_posix_ownership_is_released_only_after_sigint_and_confirmed_clean_exit(
    launches, posix_ownership, monkeypatch,
):
    assert watch.start("/game") is None
    original = launches.processes[0]
    root = watch._normalize("/game")
    ownership_root = os.path.join(root, ".editor", "asset-watcher")
    options = launches.calls[0][1]
    assert options["start_new_session"] is True
    assert options["env"] is posix_ownership.options["env"]
    assert "creationflags" not in options
    assert posix_ownership.environments == [{"CAPTURED": "original"}]
    recorded_root, process, environment, owner = posix_ownership.records[0]
    assert recorded_root == ownership_root
    assert process is original
    assert environment is options["env"]
    events = []
    send_signal, wait, release = original.send_signal, original.wait, watch.process_tree.release

    def interrupt(signum):
        events.append("signal")
        assert signum == signal.SIGINT
        send_signal(signum)

    def confirm_exit(timeout=None):
        events.append("wait")
        return wait(timeout)

    def release_after_exit(key, ownership):
        events.append("release")
        assert original.returncode == 0
        assert original.wait_timeouts == [5.0]
        return release(key, ownership)

    monkeypatch.setattr(original, "send_signal", interrupt)
    monkeypatch.setattr(original, "wait", confirm_exit)
    monkeypatch.setattr(watch.process_tree, "release", release_after_exit)
    token = _reserve()

    assert token.pause() is None

    assert events == ["signal", "wait", "release"]
    assert original.signals == [signal.SIGINT]
    assert original.kill_calls == 0
    assert posix_ownership.releases == [(ownership_root, owner)]
    assert root not in watch._WATCHERS
    assert root not in watch._OWNERSHIPS
    assert token.resume() is None
    assert len(launches.calls) == 2


def test_posix_pause_timeout_retains_ownership_without_releasing_or_killing(
    launches, posix_ownership,
):
    assert watch.start("/game") is None
    original = launches.processes[0]
    original.exit_on_terminate = False
    token = _reserve()
    owner = watch._OWNERSHIPS[token.root]

    def timeout(timeout=None):
        original.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired("watch", timeout)

    original.wait = timeout
    problem = token.pause()

    assert "blocked" in problem.lower()
    assert original.signals == [signal.SIGINT]
    assert original.wait_timeouts == [5.0]
    assert original.kill_calls == 0
    assert posix_ownership.releases == []
    assert watch._WATCHERS[token.root] is original
    assert watch._OWNERSHIPS[token.root] is owner
    assert watch._PROJECTS[token.root].owner is None
    assert token.resume() == problem
    assert "still be stopping" in problem
    assert "restart Asset Watch" in problem
    assert not watch.is_paused("/game")
    assert token.resume() is None
    assert watch.start("/game") == problem
    assert len(launches.calls) == 1
    assert posix_ownership.releases == []

    # It may finish its cooperative shutdown after Play has already failed. Starting
    # again must reclaim the retained tree before replacing it, not strand auto-watch.
    original.returncode = 0
    assert not watch.is_running("/game")
    assert watch.start("/game") is None
    assert len(launches.calls) == 2
    assert posix_ownership.releases
    assert watch.is_running("/game")


def test_posix_failed_record_keeps_the_live_child_and_blocks_restart_without_killing(
    launches, posix_ownership, monkeypatch,
):
    def fail_record(root, process, *, environment):
        raise OSError("cannot record POSIX ownership")

    monkeypatch.setattr(watch.process_tree, "record", fail_record)

    problem = watch.start("/game")

    assert "cannot record POSIX ownership" in problem
    root = watch._normalize("/game")
    original = launches.processes[0]
    assert original.returncode is None
    assert original.signals == []
    assert original.kill_calls == original.terminate_calls == 0
    assert original.wait_timeouts == []
    assert launches.calls[0][1]["stdout"].closed
    assert watch._WATCHERS[root] is original
    assert watch._PROJECTS[root].unsafe == problem
    assert posix_ownership.releases == []
    assert watch.start("/game") == problem
    token = _reserve()
    assert token.pause() == problem
    assert token.resume() == problem
    assert len(launches.calls) == 1


def test_sigint_followed_by_nonzero_exit_does_not_permit_build(launches):
    original = _register("/game")
    original.exit_on_terminate = False
    token = _reserve()

    def failed_exit(timeout=None):
        original.wait_timeouts.append(timeout)
        original.returncode = 1
        return 1

    original.wait = failed_exit
    problem = token.pause()

    assert "child writers may remain" in problem
    assert original.signals == [signal.SIGINT]
    assert original.wait_timeouts == [5.0]
    assert original.kill_calls == 0
    assert watch._PROJECTS[token.root].owner is None
    assert token.resume() == problem
    assert launches.calls == []
