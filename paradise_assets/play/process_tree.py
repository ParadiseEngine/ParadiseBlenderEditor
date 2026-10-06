"""Conservative, durable ownership of a Play stage's process tree.

Spawn with ``Popen(..., **launch_options())``, then immediately ``record(root, p)``.
Call ``stop(root)`` before replacement, including after the stage leader exits.
A non-None result MUST block replacement. ``release(root, handle)`` additionally
protects against a stale stage releasing a newer stage's ownership.

POSIX sessions are not containment: children that detach AND erase the inherited
ownership environment cannot be recovered. Without launch_options, recovery is
limited to sessions with a previously witnessed member still present. Windows
requires launch_options' suspended creation, so Job assignment precedes execution.
State lives in a private temporary directory, never in authored project assets.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_TOKEN = "PARADISE_PLAY_OWNER"
_GRACE = 0.5
_TIMEOUT = 3.0
_JOBS: dict[str, int] = {}


@dataclass(frozen=True)
class Ownership:
    project: str
    identity: str


@dataclass(frozen=True)
class _Process:
    pid: int
    parent: int
    session: int
    birth: str
    zombie: bool = False


def launch_environment() -> dict[str, str]:
    """Return ONLY fresh marker variables; merge with the caller's environment."""
    return {_TOKEN: uuid.uuid4().hex}


def launch_options(base_environment: dict[str, str] | None = None) -> dict:
    """Required Popen options, preserving preferences in base_environment."""
    environment = dict(os.environ if base_environment is None else base_environment)
    if sys.platform == "win32":
        # CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        return {"creationflags": 0x00000004 | 0x00000200 | 0x08000000, "env": environment}
    environment.update(launch_environment())
    return {"start_new_session": True, "env": environment}


def _root(project_root) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(project_root)))


def _directory() -> Path:
    # POSIX ownership/mode checks also reject a pre-created symlink in /tmp.
    user = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
    suffix = hashlib.sha256(user.encode()).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / ("paradise-play-" + suffix)
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"Unsafe ownership directory: {directory}")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise OSError(f"Ownership directory must be private and owned by this user: {directory}")
    return directory


def _path(root: str) -> Path:
    return _directory() / (hashlib.sha256(root.encode()).hexdigest() + ".json")


@contextlib.contextmanager
def _locked(path: Path):
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    windows_lock = None
    try:
        if sys.platform == "win32":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            windows_lock = msvcrt
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            if windows_lock is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                windows_lock.locking(fd, windows_lock.LK_UNLCK, 1)
        finally:
            os.close(fd)


def _load(path: Path, root: str) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise OSError("Invalid process ownership file")
        try:
            data = json.load(stream)
        except json.JSONDecodeError as exc:
            raise OSError(f"Malformed process ownership JSON; inspect {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("root") != root:
        raise OSError("Invalid process ownership metadata; inspect " + str(path))
    if not re.fullmatch(r"[0-9a-f]{32}", data.get("identity", "")):
        raise OSError("Invalid ownership identity")
    if data.get("platform") not in ("linux", "darwin", "win32"):
        raise OSError("Invalid ownership platform")
    if data["platform"] != "win32":
        if not isinstance(data.get("leader"), int) or data["leader"] <= 1:
            raise OSError("Invalid ownership leader")
        if not isinstance(data.get("members"), dict) or not data["members"]:
            raise OSError("Missing process birth identities")
        if not all(str(pid).isdigit() and int(pid) > 1 and isinstance(birth, str) and birth
                   for pid, birth in data["members"].items()):
            raise OSError("Invalid process birth identities")
        if not isinstance(data.get("boot"), str) or not data["boot"]:
            raise OSError("Missing boot identity")
        if data.get("token") is not None and not re.fullmatch(r"[0-9a-f]{32}", data["token"]):
            raise OSError("Invalid inherited ownership token")
    return data


def _save(path: Path, data: dict):
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _boot() -> str:
    if sys.platform == "linux":
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if sys.platform == "darwin":
        return subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], text=True, timeout=1).strip()
    raise OSError("Process ownership is unsupported on " + sys.platform)


def _linux_process(pid: int) -> _Process:
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rindex(")") + 2:].split()
    return _Process(pid, int(fields[1]), int(fields[3]), fields[19], fields[0] in ("Z", "X"))


class _BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid",
        "svuid", "svgid", "reserved")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
        ("jobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


def _mac_process(pid: int) -> _Process:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    info = _BSDInfo()
    size = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        error = ctypes.get_errno() or errno.ESRCH
        raise OSError(error, os.strerror(error))
    return _Process(pid, info.ppid, os.getsid(pid), f"{info.start_sec}:{info.start_usec}", info.status == 5)


def _process(pid: int) -> _Process:
    return _linux_process(pid) if sys.platform == "linux" else _mac_process(pid)


def _snapshot() -> dict[int, _Process]:
    if sys.platform == "linux":
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    elif sys.platform == "darwin":
        rows = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,uid="], text=True, timeout=1).splitlines()
        # libproc may refuse even BSD info for protected processes of other users.
        pids = [int(parts[0]) for row in rows if len(parts := row.split()) == 2
                and int(parts[1]) == os.getuid()]
    else:
        raise OSError("Process ownership is unsupported on " + sys.platform)
    result = {}
    for pid in pids:
        try:
            result[pid] = _process(pid)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ESRCH):
                raise
    return result


def _environment_token(pid: int) -> str | None:
    if sys.platform == "linux":
        environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        prefix = (_TOKEN + "=").encode()
        values = [item[len(prefix):].decode("ascii", errors="replace")
                  for item in environment if item.startswith(prefix)]
        value = values[0] if values else ""
    else:
        text = subprocess.check_output(
            ["/bin/ps", "eww", "-p", str(pid), "-o", "command="], text=True, timeout=1)
        match = re.search(r"(?:^|\s)" + _TOKEN + r"=([0-9a-f]{32})(?:\s|$)", text)
        value = match[1] if match else ""
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else None


def _token_members(snapshot: dict[int, _Process], token: str | None) -> set[int]:
    if token is None:
        return set()
    if sys.platform == "darwin":
        # One bounded ps invocation, rather than one subprocess per candidate.
        text = subprocess.check_output(
            ["/bin/ps", "eww", "-axo", "pid=,command="], text=True, timeout=1)
        pattern = re.compile(r"(?:^|\s)" + _TOKEN + "=" + re.escape(token) + r"(?:\s|$)")
        candidates = {int(parts[0]) for line in text.splitlines()
                      if len(parts := line.strip().split(None, 1)) == 2
                      and parts[0].isdigit() and int(parts[0]) in snapshot and pattern.search(parts[1])}
    else:
        candidates = None
    result = set()
    for pid, process in snapshot.items():
        if process.zombie:
            continue
        try:
            matches = pid in candidates if candidates is not None else _environment_token(pid) == token
            if matches and _same_process(process):
                result.add(pid)
        except (PermissionError, ProcessLookupError, FileNotFoundError):
            # Protected/different-UID descendants are outside POSIX containment.
            continue
        except subprocess.CalledProcessError:
            continue
    return result


def _same_process(process: _Process) -> bool:
    try:
        current = _process(process.pid)
        return current.birth == process.birth and current.session == process.session
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ESRCH):
            raise
        return False


def _owned(data: dict, snapshot: dict[int, _Process]) -> list[_Process]:
    sid = data["leader"]
    known = {pid for pid, proc in snapshot.items() if data["members"].get(str(pid)) == proc.birth}
    known.update(_token_members(snapshot, data.get("token")))
    session = {pid for pid, proc in snapshot.items() if proc.session == sid}
    # Members can disappear while the census/token scan is running. Do not let
    # stale entries turn an already-exited tree into an ambiguous live session.
    current = {pid for pid in known | session if _same_process(snapshot[pid])}
    known.intersection_update(current)
    session.intersection_update(current)
    if session & known:
        known.update(session)
    elif session:
        leader = snapshot.get(sid)
        if leader is None or leader.birth == data["members"].get(str(sid)):
            raise OSError("Cannot verify the surviving session after its leader exited; "
                          "leave ownership state intact and inspect the processes manually")
        # A different live session leader proves that this session number was reused.
    return [snapshot[pid] for pid in known if not snapshot[pid].zombie]


def _signal(process: _Process, signum: int):
    """Never signal an unchecked PID or a numeric process group."""
    fd = None
    try:
        if sys.platform == "linux" and hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            fd = os.pidfd_open(process.pid)
        current = _process(process.pid)
        if current.birth != process.birth or current.zombie:
            return
        if fd is not None:
            signal.pidfd_send_signal(fd, signum)
        else:
            # macOS has no pidfd equivalent; check exact microsecond birth just before kill.
            os.kill(process.pid, signum)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ESRCH):
            raise
    finally:
        if fd is not None:
            os.close(fd)


def record(
    project_root, process: subprocess.Popen, *, environment: dict[str, str] | None = None,
) -> Ownership:
    """Persist ownership, or raise OSError; never overwrite an unreclaimed stage.

    Pass the exact launch environment to retain the nonce even if a fast stage
    has already exited. Recording does not poll/reap the caller's child.

    Windows callers MUST have used launch_options (the child is suspended until
    this function assigns it to a kill-on-close Job). A failed record means the
    caller must kill and reap its newly spawned child, and must not launch again.
    """
    try:
        root = _root(project_root)
        path = _path(root)
        with _locked(path):
            if _load(path, root) is not None:
                raise OSError("An earlier stage still owns this project; call stop before launching")
            identity = uuid.uuid4().hex
            data = {"version": 1, "root": root, "identity": identity, "platform": sys.platform}
            if sys.platform == "win32":
                _windows_record(path, data, process)
            else:
                boot = _boot()
                snapshot = _snapshot()
                token = environment.get(_TOKEN) if environment is not None else None
                if token is not None and not re.fullmatch(r"[0-9a-f]{32}", token):
                    raise OSError("Invalid launch ownership token")
                leader = snapshot.get(process.pid) if getattr(process, "returncode", None) is None else None
                members = {str(process.pid): "unobserved-" + identity}
                if leader is not None:
                    if leader.session != leader.pid:
                        raise OSError("Play processes must launch with start_new_session=True")
                    if token is None and not leader.zombie:
                        with contextlib.suppress(
                            FileNotFoundError, ProcessLookupError, subprocess.CalledProcessError,
                        ):
                            token = _environment_token(leader.pid)
                    members.update({str(pid): member.birth for pid, member in snapshot.items()
                                    if member.session == leader.pid})
                else:
                    # A reaped leader's PID may already be unrelated. Only the
                    # inherited nonce can establish ownership of unseen children.
                    members.update({str(pid): snapshot[pid].birth
                                    for pid in _token_members(snapshot, token)})
                data.update(boot=boot, leader=process.pid, token=token, members=members)
                _save(path, data)
            return Ownership(root, identity)
    except (ValueError, TypeError, subprocess.SubprocessError) as exc:
        raise OSError(f"Could not record process ownership: {exc}") from exc


def _stop_posix(path: Path, data: dict):
    if data["boot"] != _boot():
        return
    deadline = time.monotonic() + _TIMEOUT
    gentle_until = time.monotonic() + _GRACE
    sent = set()
    empty_seen = False
    while True:
        owned = _owned(data, _snapshot())
        if not owned:
            if empty_seen:
                return
            empty_seen = True
            time.sleep(0.05)
            continue
        empty_seen = False
        if time.monotonic() >= deadline:
            raise OSError("Processes still alive after termination: " + ", ".join(str(p.pid) for p in owned))
        data["members"].update({str(proc.pid): proc.birth for proc in owned})
        # Persist newly witnessed members before any signal can cause leader exit.
        _save(path, data)
        signum = signal.SIGTERM if time.monotonic() < gentle_until else signal.SIGKILL
        for proc in owned:
            key = (proc.pid, proc.birth, signum)
            if key not in sent:
                _signal(proc, signum)
                sent.add(key)
        time.sleep(0.05)


def _stop(project_root, ownership: Ownership | None) -> str | None:
    try:
        root = _root(project_root)
        path = _path(root)
        with _locked(path):
            data = _load(path, root)
            if data is None:
                return None
            if ownership is not None and (
                ownership.project != root or ownership.identity != data["identity"]
            ):
                return None
            if data["platform"] != sys.platform:
                raise OSError("Ownership was recorded on another platform; inspect " + str(path))
            if sys.platform == "win32":
                _windows_stop(data)
            else:
                _stop_posix(path, data)
            path.unlink()
        return None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        return (
            "Cannot confirm the previous Play tree stopped; resolve the cleanup error "
            f"and retry Stop before launching again: {exc}"
        )


def stop(project_root) -> str | None:
    """Reclaim a previous tree without in-memory state; None confirms cleanup."""
    return _stop(project_root, None)


def release(project_root, ownership: Ownership) -> str | None:
    """Clean descendants after stage exit, unless a newer stage owns the root."""
    return _stop(project_root, ownership)


# Windows Jobs provide kernel-enforced inheritance and crash cleanup, unlike taskkill.
def _kernel():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL,
        ),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL,
        ),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def _job_name(data: dict) -> str:
    return "Local\\ParadisePlay-" + data["identity"]


def _windows_record(path: Path, data: dict, process: subprocess.Popen):
    from ctypes import wintypes

    class Limits(ctypes.Structure):
        _fields_ = [("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                    ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, _job_name(data))
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: never adopt an unknown Job.
            raise OSError("Unexpected existing process Job")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        _save(path, data)
        native = ctypes.WinDLL("ntdll")
        native.NtResumeProcess.argtypes = [wintypes.HANDLE]
        native.NtResumeProcess.restype = ctypes.c_long
        if native.NtResumeProcess(int(process._handle)) != 0:
            raise OSError("Could not resume the stage after Job assignment")
        _JOBS[data["identity"]] = job
        job = None
    finally:
        if job:
            kernel.CloseHandle(job)


def _windows_stop(data: dict):
    class Accounting(ctypes.Structure):
        _fields_ = [("times", ctypes.c_int64 * 4), ("faults", ctypes.c_uint32),
                    ("total", ctypes.c_uint32), ("active", ctypes.c_uint32),
                    ("terminated", ctypes.c_uint32)]

    kernel = _kernel()
    job = kernel.OpenJobObjectW(0x0004 | 0x0008, False, _job_name(data))
    if not job:
        error = ctypes.get_last_error()
        if error == 2:  # Job no longer exists: kill-on-close has reclaimed its processes.
            return
        raise ctypes.WinError(error)
    try:
        if not kernel.TerminateJobObject(job, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + _TIMEOUT
        while True:
            accounting = Accounting()
            if not kernel.QueryInformationJobObject(
                job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.active == 0:
                break
            if time.monotonic() >= deadline:
                raise OSError(f"Job still contains {accounting.active} active processes")
            time.sleep(0.05)
        retained = _JOBS.pop(data["identity"], None)
        if retained:
            kernel.CloseHandle(retained)
    finally:
        kernel.CloseHandle(job)
