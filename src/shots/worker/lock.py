"""Overlap lock for worker cycles: one cycle at a time per machine (boot run vs 3-hourly run vs manual).

The lock is an OS lock on `data/worker.lock` (next to the DB file), held for the life of the process:
a byte-range lock on Windows (msvcrt), `flock` elsewhere. The operating system releases it when the
process exits, crashes or is killed by Task Scheduler, so a dead holder can never block later cycles:
a lock file left behind by a dead process is detected as stale and taken over.

POSIX note: `flock`, not `fcntl.lockf`. A POSIX record lock belongs to the process and is dropped as
soon as the process closes ANY descriptor of the file, so merely reading the holder record through a
second descriptor released it (found on Ubuntu, 2026-10-12: two cycles could run at once). A `flock`
lock belongs to the open file description and is only released by unlocking, closing that descriptor
or the process ending. The file stays in place between cycles (an empty record
means free). It also records the holder (pid, host, start time) so
`status` can show who holds it, whether that process is alive and for how long it has held it.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCK_OFFSET = 1 << 16  # Windows: lock one byte past the info record, so readers can still read the holder info
INFO_SIZE = 512
DEFAULT_STALE_AFTER = timedelta(hours=6)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def pid_alive(pid: int | None) -> bool:
    """Is a process with this id running on this machine?"""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists, owned by someone else
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (ended, not yet reaped by its parent) still answers signal 0 but runs nothing and has
    # already dropped its locks: not alive. Linux exposes the state in /proc; elsewhere assume alive.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rsplit(b")", 1)[1].split()[0]
        return state not in (b"Z", b"X")
    except (OSError, IndexError):
        return True


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # see the module docstring: not lockf
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass
class LockState:
    """state: free (no lock file), stale (file left by a holder that is gone; the next cycle takes it
    over) or held (a live process holds it; `suspect` when its pid looks dead or it has held the lock
    longer than stale_after, e.g. a hung manual run)."""
    state: str
    path: str
    pid: int | None = None
    host: str | None = None
    started_at: str | None = None
    age_seconds: float | None = None
    pid_alive: bool | None = None
    suspect: bool = False

    def describe(self) -> str:
        if self.state == "free":
            return "free"
        who = f"pid {self.pid} on {self.host}, since {self.started_at} UTC"
        if self.age_seconds is not None:
            who += f" ({self.age_seconds / 60:.0f} min)"
        if self.state == "stale":
            return f"stale (left by {who}; process gone, will be taken over)"
        return f"held by {who}" + (" - SUSPECT: holder looks hung or dead" if self.suspect else "")


class WorkerLock:
    def __init__(self, path: Path | str, stale_after: timedelta = DEFAULT_STALE_AFTER):
        self.path = Path(path)
        self.stale_after = stale_after
        self._fd: int | None = None
        self.taken_over: dict | None = None  # holder info of a stale lock this process took over

    # ------------------------------------------------------------------ info record

    def _read_info(self) -> dict:
        try:
            # unbuffered: a buffered read may ask for bytes up to the locked one, which Windows refuses
            with open(self.path, "rb", buffering=0) as fh:
                raw = fh.read(INFO_SIZE).decode("utf-8", "replace").strip("\x00 \r\n")
            return json.loads(raw) if raw else {}
        except (OSError, ValueError):
            return {}

    def _write_info(self) -> None:
        info = {"pid": os.getpid(), "host": socket.gethostname()[:80],
                "started_at": utcnow().isoformat(timespec="seconds")}
        data = json.dumps(info).encode("utf-8").ljust(INFO_SIZE - 1) + b"\n"
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, data)
        os.fsync(self._fd)

    def _state_from(self, info: dict, state: str) -> LockState:
        started = info.get("started_at")
        age = None
        if started:
            try:
                age = (utcnow() - datetime.fromisoformat(started)).total_seconds()
            except ValueError:
                pass
        same_host = info.get("host") == socket.gethostname()[:80]
        alive = pid_alive(info.get("pid")) if same_host else None
        suspect = state == "held" and (alive is False or (age is not None and age > self.stale_after.total_seconds()))
        return LockState(state, str(self.path), info.get("pid"), info.get("host"), started, age, alive, suspect)

    # ------------------------------------------------------------------ acquire / release

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Take the lock without waiting. False if another live process holds it."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        if not _try_lock(fd):
            os.close(fd)
            return False
        self._fd = fd
        previous = self._read_info()  # read under the lock: a clean release leaves an empty record
        if previous.get("pid"):
            self.taken_over = previous  # the previous holder died without releasing (stale lock)
        self._write_info()
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b" " * (INFO_SIZE - 1) + b"\n")  # clear the holder record while still locked
            _unlock(fd)
        finally:
            os.close(fd)
        # The file is kept (empty record = free): deleting it while another process has it open would
        # let a third process lock a new file with the same name (two holders on POSIX).

    def inspect(self) -> LockState:
        """Current state without keeping the lock."""
        if self._fd is not None:
            return self._state_from(self._read_info(), "held")
        if not self.path.exists():
            return LockState("free", str(self.path))
        info = self._read_info()
        try:
            fd = os.open(self.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            return LockState("free", str(self.path))
        try:
            if _try_lock(fd):
                _unlock(fd)
                return self._state_from(info, "stale") if info.get("pid") else LockState("free", str(self.path))
            return self._state_from(info, "held")
        finally:
            os.close(fd)

    def __enter__(self) -> "WorkerLock":
        if not self.acquire():
            raise BlockingIOError(f"worker lock {self.path} is held: {self.inspect().describe()}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()
