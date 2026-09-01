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

from paradise_assets import watch


class FakeProcess:
    """Just enough of ``Popen``: alive until it is told otherwise."""

    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _register(root: str) -> FakeProcess:
    """Put a live fake in the table, the way a successful start would."""
    process = FakeProcess()
    watch._WATCHERS[watch._normalize(root)] = process
    return process


def teardown_function(_function):
    watch._WATCHERS.clear()


def test_nothing_is_running_to_begin_with():
    assert not watch.is_running("/some/project")
    assert watch.watched_roots() == []


def test_a_registered_watcher_reads_as_running():
    _register("/some/project")

    assert watch.is_running("/some/project")
    assert len(watch.watched_roots()) == 1


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
    assert not watch.is_running("/some/project")


def test_stop_kills_a_watcher_that_ignores_terminate():
    # Blender must not block on a child that will not go. The wait is short and the kill is
    # unconditional after it.
    process = _register("/some/project")

    def stubborn(timeout=None):
        raise __import__("subprocess").TimeoutExpired(cmd="watch", timeout=timeout)

    process.wait = stubborn
    watch.stop("/some/project")

    assert process.killed


def test_stop_all_clears_every_project():
    first, second = _register("/project/one"), _register("/project/two")

    watch.stop_all()

    assert first.terminated and second.terminated
    assert watch.watched_roots() == []


def test_stopping_something_that_is_not_running_is_harmless():
    watch.stop("/never/started")   # must not raise
    assert watch.watched_roots() == []


def test_log_paths_differ_per_project():
    # Two checkouts of one game is a normal thing to have; a shared log would interleave them.
    first = watch.log_path("/checkout/one/shiningpie")
    second = watch.log_path("/checkout/two/shiningpie")

    assert first != second
    assert "shiningpie" in os.path.basename(first)


def test_status_line_reports_not_watching_when_it_is_not():
    assert watch.status_line("/some/project") == "Not watching."
