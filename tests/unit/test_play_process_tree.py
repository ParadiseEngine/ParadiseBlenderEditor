"""Durable ownership, identity reuse, and cleanup after the stage leader exits."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from paradise_assets.play import process_tree as tree


@pytest.fixture
def state_directory(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(tree, "_directory", lambda: directory)
    return directory


@pytest.fixture
def posix_tree(state_directory, monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "linux")
    monkeypatch.setattr(tree, "_locked", lambda path: contextlib.nullcontext())
    monkeypatch.setattr(tree, "_boot", lambda: "boot-one")
    monkeypatch.setattr(tree.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(tree.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(tree, "_environment_token", lambda pid: None)
    monkeypatch.setattr(tree, "_token_members", lambda snapshot, token: set())
    processes = {100: tree._Process(100, 1, 100, "birth-100")}
    monkeypatch.setattr(tree, "_snapshot", lambda: dict(processes))

    def process(pid):
        if pid not in processes:
            raise ProcessLookupError(errno.ESRCH, "gone")
        return processes[pid]

    monkeypatch.setattr(tree, "_process", process)
    signals = []

    def send(proc, signum):
        signals.append((proc.pid, signum))
        processes.pop(proc.pid, None)

    monkeypatch.setattr(tree, "_signal", send)
    monkeypatch.setattr(tree.time, "sleep", lambda seconds: None)
    return SimpleNamespace(processes=processes, signals=signals, child=SimpleNamespace(pid=100))


def test_launch_environment_is_only_a_unique_marker():
    first, second = tree.launch_environment(), tree.launch_environment()
    assert set(first) == {tree._TOKEN}
    assert len(first[tree._TOKEN]) == 32
    assert first != second


def test_posix_options_preserve_preference_environment(monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "linux")
    base = {"PATH": "/custom/dotnet", "PARADISE": "preferred"}
    options = tree.launch_options(base)
    assert options["start_new_session"] is True
    assert options["env"]["PATH"] == "/custom/dotnet"
    assert options["env"]["PARADISE"] == "preferred"
    assert tree._TOKEN in options["env"]
    assert tree._TOKEN not in base


def test_windows_options_suspend_before_job_assignment(monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "win32")
    options = tree.launch_options({"PATH": "custom"})
    assert options["creationflags"] & 0x4  # CREATE_SUSPENDED
    assert options["creationflags"] & 0x200  # CREATE_NEW_PROCESS_GROUP
    assert options["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
    assert "start_new_session" not in options
    assert options["env"] == {"PATH": "custom"}


def test_record_is_durable_without_retaining_popen(tmp_path, posix_tree):
    ownership = tree.record(tmp_path, posix_tree.child)
    path = tree._path(tree._root(tmp_path))
    data = json.loads(path.read_text())
    assert data["members"] == {"100": "birth-100"}
    assert data["identity"] == ownership.identity
    assert data["boot"] == "boot-one"
    del ownership
    assert tree.stop(tmp_path) is None
    assert posix_tree.signals == [(100, signal.SIGTERM)]
    assert not path.exists()


def test_record_refuses_to_overwrite_previous_stage(tmp_path, posix_tree):
    tree.record(tmp_path, posix_tree.child)
    with pytest.raises(OSError, match="earlier stage"):
        tree.record(tmp_path, posix_tree.child)
    assert not posix_tree.signals


def test_record_requires_an_isolated_session(tmp_path, posix_tree):
    posix_tree.processes[100] = tree._Process(100, 1, 99, "birth-100")
    with pytest.raises(OSError, match="start_new_session"):
        tree.record(tmp_path, posix_tree.child)


def test_record_does_not_reap_the_child(tmp_path, posix_tree):
    child = SimpleNamespace(pid=100, poll=Mock(side_effect=AssertionError("must not reap")))
    tree.record(tmp_path, child)
    child.poll.assert_not_called()


def test_record_accepts_fast_zombie_stage(tmp_path, posix_tree):
    posix_tree.processes[100] = tree._Process(100, 1, 100, "birth-100", True)
    environment = {tree._TOKEN: "a" * 32}
    ownership = tree.record(tmp_path, posix_tree.child, environment=environment)
    assert tree.release(tmp_path, ownership) is None
    assert not posix_tree.signals


def test_record_accepts_already_reaped_empty_stage(tmp_path, posix_tree):
    posix_tree.processes.clear()
    child = SimpleNamespace(pid=100, returncode=0)
    ownership = tree.record(tmp_path, child, environment={tree._TOKEN: "a" * 32})
    assert tree.release(tmp_path, ownership) is None


def test_record_reaped_stage_uses_nonce_not_reused_leader(tmp_path, posix_tree, monkeypatch):
    posix_tree.processes[100] = tree._Process(100, 1, 100, "unrelated-new-birth")
    posix_tree.processes[101] = tree._Process(101, 1, 101, "detached-child")
    monkeypatch.setattr(tree, "_token_members",
                        lambda snapshot, token: {101} & snapshot.keys())
    child = SimpleNamespace(pid=100, returncode=0)
    ownership = tree.record(tmp_path, child, environment={tree._TOKEN: "a" * 32})
    assert tree.release(tmp_path, ownership) is None
    assert posix_tree.signals == [(101, signal.SIGTERM)]
    assert posix_tree.processes[100].birth == "unrelated-new-birth"


def test_release_of_completed_stage_preserves_new_ownership(tmp_path, posix_tree):
    previous = tree.record(tmp_path, posix_tree.child)
    assert tree.release(tmp_path, previous) is None
    posix_tree.processes[100] = tree._Process(100, 1, 100, "replacement-birth")
    replacement = tree.record(tmp_path, posix_tree.child)
    posix_tree.signals.clear()
    assert previous.identity != replacement.identity
    assert tree.release(tmp_path, previous) is None
    assert not posix_tree.signals
    assert tree.release(tmp_path, replacement) is None
    assert posix_tree.signals == [(100, signal.SIGTERM)]


def test_record_converts_serialization_errors_to_oserror(tmp_path, posix_tree, monkeypatch):
    monkeypatch.setattr(tree, "_save", Mock(side_effect=ValueError("invalid data")))
    with pytest.raises(OSError, match="Could not record"):
        tree.record(tmp_path, posix_tree.child)


def test_exited_leader_does_not_forget_witnessed_descendants(tmp_path, posix_tree):
    posix_tree.processes[101] = tree._Process(101, 100, 100, "birth-101")
    ownership = tree.record(tmp_path, posix_tree.child)
    del posix_tree.processes[100]
    assert tree.release(tmp_path, ownership) is None
    assert posix_tree.signals == [(101, signal.SIGTERM)]


def test_new_members_are_discovered_from_a_verified_session(tmp_path, posix_tree):
    tree.record(tmp_path, posix_tree.child)
    posix_tree.processes[101] = tree._Process(101, 100, 100, "birth-101")
    assert tree.stop(tmp_path) is None
    assert {pid for pid, sig in posix_tree.signals} == {100, 101}


def test_nonce_recovers_late_child_after_leader_exit(tmp_path, posix_tree, monkeypatch):
    token = "a" * 32
    monkeypatch.setattr(tree, "_environment_token", lambda pid: token)
    tree.record(tmp_path, posix_tree.child)
    path = tree._path(tree._root(tmp_path))
    assert json.loads(path.read_text())["token"] == token
    posix_tree.processes.clear()
    posix_tree.processes[101] = tree._Process(101, 1, 100, "birth-101")
    monkeypatch.setattr(
        tree, "_token_members", lambda snapshot, nonce: set(snapshot) if nonce == token else set(),
    )
    assert tree.stop(tmp_path) is None
    assert posix_tree.signals == [(101, signal.SIGTERM)]


def test_unverified_orphan_blocks_replacement_without_signals(tmp_path, posix_tree):
    tree.record(tmp_path, posix_tree.child)
    posix_tree.processes.clear()
    posix_tree.processes[101] = tree._Process(101, 1, 100, "unwitnessed")
    error = tree.stop(tmp_path)
    assert "replacement is blocked" in error
    assert "Cannot verify" in error
    assert not posix_tree.signals
    assert tree._path(tree._root(tmp_path)).exists()


def test_reused_unrelated_leader_is_not_killed(tmp_path, posix_tree):
    tree.record(tmp_path, posix_tree.child)
    posix_tree.processes[100] = tree._Process(100, 1, 100, "unrelated-new-birth")
    posix_tree.processes[105] = tree._Process(105, 100, 100, "unrelated-child")
    assert tree.stop(tmp_path) is None
    assert not posix_tree.signals


def test_empty_session_is_success_even_without_original_leader(tmp_path, posix_tree):
    tree.record(tmp_path, posix_tree.child)
    posix_tree.processes.clear()
    assert tree.stop(tmp_path) is None


def test_zombie_leader_and_children_do_not_block_cleanup(tmp_path, posix_tree):
    tree.record(tmp_path, posix_tree.child)
    posix_tree.processes[100] = tree._Process(100, 1, 100, "birth-100", True)
    posix_tree.processes[101] = tree._Process(101, 1, 100, "birth-101", True)
    assert tree.stop(tmp_path) is None
    assert not posix_tree.signals


def test_reboot_invalidates_old_ownership_without_signals(tmp_path, posix_tree, monkeypatch):
    tree.record(tmp_path, posix_tree.child)
    monkeypatch.setattr(tree, "_boot", lambda: "different-boot")
    assert tree.stop(tmp_path) is None
    assert not posix_tree.signals


def test_stale_release_does_not_stop_replacement(tmp_path, posix_tree):
    ownership = tree.record(tmp_path, posix_tree.child)
    stale = tree.Ownership(ownership.project, "f" * 32)
    assert tree.release(tmp_path, stale) is None
    assert not posix_tree.signals
    assert tree._path(tree._root(tmp_path)).exists()


def test_corrupt_state_is_not_removed_or_signaled(tmp_path, posix_tree):
    path = tree._path(tree._root(tmp_path))
    path.write_text("not json")
    assert "replacement is blocked" in tree.stop(tmp_path)
    assert path.read_text() == "not json"
    assert not posix_tree.signals


def test_unreadable_state_returns_actionable_failure(tmp_path, posix_tree, monkeypatch):
    monkeypatch.setattr(tree, "_load", Mock(side_effect=PermissionError("ownership file denied")))
    assert "ownership file denied" in tree.stop(tmp_path)
    assert not posix_tree.signals


def test_persistence_failure_prevents_any_signal(tmp_path, posix_tree, monkeypatch):
    tree.record(tmp_path, posix_tree.child)
    monkeypatch.setattr(tree, "_save", Mock(side_effect=OSError("disk full")))
    assert "disk full" in tree.stop(tmp_path)
    assert not posix_tree.signals
    assert tree._path(tree._root(tmp_path)).exists()


def test_incomplete_cleanup_keeps_metadata_and_escalates(tmp_path, posix_tree, monkeypatch):
    tree.record(tmp_path, posix_tree.child)
    clock = [0.0]
    monkeypatch.setattr(tree.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tree.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(tree, "_signal", lambda proc, sig: posix_tree.signals.append((proc.pid, sig)))
    error = tree.stop(tmp_path)
    assert "still alive" in error
    assert (100, signal.SIGTERM) in posix_tree.signals
    assert (100, signal.SIGKILL) in posix_tree.signals
    assert clock[0] < 4
    assert tree._path(tree._root(tmp_path)).exists()


def test_identity_is_checked_again_before_pidfd_signal(monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "linux")
    monkeypatch.setattr(tree.os, "pidfd_open", Mock(return_value=55), raising=False)
    send = Mock()
    close = Mock()
    monkeypatch.setattr(tree.signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(tree.os, "close", close)
    monkeypatch.setattr(tree, "_process", lambda pid: tree._Process(pid, 1, pid, "new-birth"))
    tree._signal(tree._Process(100, 1, 100, "old-birth"), signal.SIGTERM)
    send.assert_not_called()
    close.assert_called_once_with(55)


def test_linux_uses_verified_pidfd_not_numeric_kill(monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "linux")
    process = tree._Process(100, 1, 100, "birth")
    monkeypatch.setattr(tree, "_process", lambda pid: process)
    monkeypatch.setattr(tree.os, "pidfd_open", Mock(return_value=55), raising=False)
    send, numeric = Mock(), Mock()
    monkeypatch.setattr(tree.signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(tree.os, "kill", numeric)
    monkeypatch.setattr(tree.os, "close", Mock())
    tree._signal(process, signal.SIGTERM)
    send.assert_called_once_with(55, signal.SIGTERM)
    numeric.assert_not_called()


def test_mac_uses_exact_birth_identity_before_signal(monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "darwin")
    original = tree._Process(100, 1, 100, "1000:123456")
    monkeypatch.setattr(tree, "_process", lambda pid: tree._Process(pid, 1, pid, "1000:123457"))
    numeric = Mock()
    monkeypatch.setattr(tree.os, "kill", numeric)
    tree._signal(original, signal.SIGTERM)
    numeric.assert_not_called()


def test_mac_reads_token_members_in_one_bounded_ps_call(monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "darwin")
    token = "a" * 32
    process = tree._Process(101, 1, 100, "birth")
    read = Mock(return_value=f"101 python {tree._TOKEN}={token}\n102 other\n")
    monkeypatch.setattr(tree.subprocess, "check_output", read)
    monkeypatch.setattr(tree, "_process", lambda pid: process)
    assert tree._token_members({101: process}, token) == {101}
    assert read.call_count == 1
    assert read.call_args.kwargs["timeout"] == 1


def test_mac_snapshot_does_not_inspect_protected_foreign_processes(monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "darwin")
    monkeypatch.setattr(tree.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(tree.subprocess, "check_output", Mock(return_value="1 0\n101 501\n"))
    process = tree._Process(101, 1, 101, "birth")
    read = Mock(return_value=process)
    monkeypatch.setattr(tree, "_process", read)
    assert tree._snapshot() == {101: process}
    read.assert_called_once_with(101)


@pytest.fixture
def windows_kernel(monkeypatch):
    kernel = SimpleNamespace(
        CreateJobObjectW=Mock(return_value=201),
        OpenJobObjectW=Mock(return_value=202),
        SetInformationJobObject=Mock(return_value=True),
        AssignProcessToJobObject=Mock(return_value=True),
        TerminateJobObject=Mock(return_value=True),
        QueryInformationJobObject=Mock(return_value=True),
        CloseHandle=Mock(return_value=True),
    )
    monkeypatch.setattr(tree, "_kernel", lambda: kernel)
    monkeypatch.setattr(tree.ctypes, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setattr(tree.ctypes, "WinError", lambda code: OSError(code, "native error"), raising=False)
    monkeypatch.setattr(tree, "_JOBS", {})
    return kernel


def test_windows_assigns_and_persists_before_resuming(tmp_path, windows_kernel, monkeypatch):
    events = []
    windows_kernel.AssignProcessToJobObject.side_effect = lambda *args: events.append("assign") or True
    monkeypatch.setattr(tree, "_save", lambda *args: events.append("persist"))
    resume = Mock(side_effect=lambda *args: events.append("resume") or 0)
    monkeypatch.setattr(
        tree.ctypes, "WinDLL", lambda *args: SimpleNamespace(NtResumeProcess=resume), raising=False,
    )
    data = {"identity": "a" * 32}
    tree._windows_record(tmp_path / "owner.json", data, SimpleNamespace(_handle=99))
    assert events == ["assign", "persist", "resume"]
    assert tree._JOBS[data["identity"]] == 201
    windows_kernel.AssignProcessToJobObject.assert_called_once_with(201, 99)
    windows_kernel.CloseHandle.assert_not_called()


def test_windows_failed_record_closes_kill_on_close_job(tmp_path, windows_kernel, monkeypatch):
    monkeypatch.setattr(tree, "_save", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        tree._windows_record(tmp_path / "owner.json", {"identity": "b" * 32}, SimpleNamespace(_handle=99))
    windows_kernel.CloseHandle.assert_called_once_with(201)
    assert not tree._JOBS


def test_windows_recovers_named_job_without_in_memory_handle(windows_kernel):
    data = {"identity": "a" * 32}
    tree._windows_stop(data)
    windows_kernel.OpenJobObjectW.assert_called_once_with(12, False, "Local\\ParadisePlay-" + "a" * 32)
    windows_kernel.TerminateJobObject.assert_called_once_with(202, 1)
    windows_kernel.CloseHandle.assert_called_once_with(202)


def test_windows_stop_closes_retained_job_handle(windows_kernel):
    data = {"identity": "a" * 32}
    tree._JOBS[data["identity"]] = 201
    tree._windows_stop(data)
    assert not tree._JOBS
    assert {call.args[0] for call in windows_kernel.CloseHandle.call_args_list} == {201, 202}


def test_windows_missing_job_is_already_reclaimed(windows_kernel, monkeypatch):
    windows_kernel.OpenJobObjectW.return_value = 0
    monkeypatch.setattr(tree.ctypes, "get_last_error", lambda: 2)
    tree._windows_stop({"identity": "a" * 32})
    windows_kernel.TerminateJobObject.assert_not_called()


def test_windows_access_denied_is_not_reported_as_success(windows_kernel, monkeypatch):
    windows_kernel.OpenJobObjectW.return_value = 0
    monkeypatch.setattr(tree.ctypes, "get_last_error", lambda: 5)
    with pytest.raises(OSError):
        tree._windows_stop({"identity": "a" * 32})
    windows_kernel.TerminateJobObject.assert_not_called()


def test_windows_waits_for_actual_job_exit(windows_kernel, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(tree.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tree.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def still_active(job, kind, pointer, size, length):
        pointer._obj.active = 1
        return True

    windows_kernel.QueryInformationJobObject.side_effect = still_active
    with pytest.raises(OSError, match="still contains 1"):
        tree._windows_stop({"identity": "a" * 32})
    assert clock[0] < 4
    windows_kernel.CloseHandle.assert_called_once_with(202)


def _wait_for(path: Path):
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"Child did not create {path}")
        time.sleep(0.01)


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="real POSIX sessions")
@pytest.mark.parametrize("detached", [False, True])
def test_real_orphan_created_after_record_is_reclaimed(tmp_path, state_directory, detached):
    trigger, ready = tmp_path / "go", tmp_path / "child.pid"
    child_code = (
        "import os,signal,time,pathlib; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    leader_code = (
        "import pathlib,subprocess,sys,time\n"
        f"while not pathlib.Path({str(trigger)!r}).exists(): time.sleep(.01)\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session={detached!r})\n"
        f"while not pathlib.Path({str(ready)!r}).exists(): time.sleep(.01)\n"
    )
    options = tree.launch_options()
    leader = subprocess.Popen([sys.executable, "-c", leader_code], **options)
    child_identity = None
    try:
        tree.record(tmp_path, leader, environment=options["env"])
        trigger.touch()
        _wait_for(ready)
        child_identity = tree._process(int(ready.read_text()))
        leader.wait(timeout=5)
        assert leader.returncode == 0
        # No Popen or ownership handle is needed for this reload/crash-recovery path.
        assert tree.stop(tmp_path) is None
        try:
            remaining = tree._process(child_identity.pid)
        except (FileNotFoundError, ProcessLookupError):
            remaining = None
        assert remaining is None or remaining.zombie or remaining.birth != child_identity.birth
        assert not tree._path(tree._root(tmp_path)).exists()
    finally:
        if leader.poll() is None:
            leader.kill()
        leader.wait(timeout=5)
        if child_identity is not None:
            tree._signal(child_identity, signal.SIGKILL)
        tree.stop(tmp_path)


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="real POSIX stage")
def test_real_fast_stage_can_be_recorded_after_wait(tmp_path, state_directory):
    options = tree.launch_options()
    child = subprocess.Popen([sys.executable, "-c", "pass"], **options)
    child.wait(timeout=5)
    ownership = tree.record(tmp_path, child, environment=options["env"])
    assert tree.release(tmp_path, ownership) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX private state permissions")
def test_private_state_rejects_symlink_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(tree.tempfile, "gettempdir", lambda: str(tmp_path))
    directory = tree._directory()
    directory.rmdir()
    target = tmp_path / "other"
    target.mkdir()
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="Unsafe ownership directory"):
        tree._directory()


@pytest.mark.parametrize("failure", [None, "acquire", "unlock"])
def test_windows_lock_explicitly_unlocks_before_close(tmp_path, monkeypatch, failure):
    events = []

    def locking(fd, mode, count):
        assert fd == 7 and count == 1
        operation = "acquire" if mode == 1 else "unlock"
        events.append(operation)
        if operation == failure:
            raise OSError(operation + " failed")

    def seek(fd, offset, origin):
        assert (fd, offset, origin) == (7, 0, os.SEEK_SET)
        events.append("seek")

    native = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0, locking=locking)
    monkeypatch.setattr(tree.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "msvcrt", native)
    monkeypatch.setattr(tree.os, "open", lambda *args: 7)
    monkeypatch.setattr(tree.os, "fstat", lambda fd: SimpleNamespace(st_size=1))
    monkeypatch.setattr(tree.os, "lseek", seek)
    monkeypatch.setattr(tree.os, "close", lambda fd: events.append("close"))
    expected_error = pytest.raises(OSError, match=failure) if failure else contextlib.nullcontext()
    with expected_error, tree._locked(tmp_path / "owner.json"):
        events.append("body")
    if failure == "acquire":
        assert events == ["seek", "acquire", "close"]
    else:
        assert events == ["seek", "acquire", "body", "seek", "unlock", "close"]


def test_mac_boot_identity_is_not_adjustable_wall_clock(monkeypatch):
    monkeypatch.setattr(tree.sys, "platform", "darwin")
    query = Mock(return_value="stable-boot-session-uuid\n")
    monkeypatch.setattr(tree.subprocess, "check_output", query)
    assert tree._boot() == "stable-boot-session-uuid"
    query.assert_called_once_with(
        ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], text=True, timeout=1)


@pytest.mark.parametrize("witnessed", [False, True])
def test_session_members_exiting_during_census_do_not_block_cleanup(
        tmp_path, posix_tree, monkeypatch, witnessed):
    child = tree._Process(101, 1, 100, "birth-101")
    if witnessed:
        posix_tree.processes[101] = child
    tree.record(tmp_path, posix_tree.child)
    # The census saw an orphan, but it exited before identity revalidation.
    posix_tree.processes.clear()
    monkeypatch.setattr(tree, "_snapshot", Mock(side_effect=[{101: child}, {}]))
    assert tree.stop(tmp_path) is None
    assert not posix_tree.signals
    assert not tree._path(tree._root(tmp_path)).exists()
