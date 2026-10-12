"""Overlap lock: one cycle at a time across processes; a dead holder's lock is stale and taken over."""

import os
import signal
import subprocess
import sys
import time
from datetime import timedelta

import pytest

from shots.worker.lock import WorkerLock, pid_alive

HOLDER = """
import os, sys
from shots.worker.lock import WorkerLock
lock = WorkerLock(sys.argv[1])
print("locked" if lock.acquire() else "busy", os.getpid(), flush=True)
sys.stdin.readline()  # hold until the parent says so (or kills us)
lock.release()
print("released", flush=True)
"""


@pytest.fixture
def holder(tmp_path):
    """Start another process holding the lock; start() -> (process, pid of the interpreter itself:
    on Windows a venv python.exe is a launcher that runs the real interpreter as a child)."""
    path = tmp_path / "worker.lock"
    procs = []

    def start():
        p = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        state, pid = p.stdout.readline().split()
        assert state == "locked"
        procs.append((p, int(pid)))
        return p, int(pid)

    yield path, start
    for p, pid in procs:
        if pid_alive(pid):
            os.kill(pid, signal.SIGTERM)
        if p.poll() is None:
            p.kill()
            p.wait()


def kill_and_wait(pid):
    os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows: no clean release
    for _ in range(100):
        if not pid_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} still alive")


def test_acquire_release_and_reacquire(tmp_path):
    lock = WorkerLock(tmp_path / "data" / "worker.lock")
    assert lock.inspect().state == "free"
    assert lock.acquire() and lock.held
    st = lock.inspect()
    assert st.state == "held" and st.pid == os.getpid() and st.pid_alive and not st.suspect
    lock.release()
    assert not lock.held and lock.inspect().state == "free"
    with WorkerLock(lock.path) as again:
        assert again.held and again.taken_over is None  # a clean release is not a stale lock


def test_second_process_cannot_take_a_held_lock(holder):
    path, start = holder
    p, pid = start()
    lock = WorkerLock(path)
    assert not lock.acquire()
    st = lock.inspect()
    assert st.state == "held" and st.pid == pid and st.pid_alive is True
    assert "held by pid" in st.describe()
    with pytest.raises(BlockingIOError):
        with WorkerLock(path):
            pass
    p.stdin.write("\n")
    p.stdin.flush()
    assert p.stdout.readline().strip() == "released"
    p.wait(timeout=30)
    assert lock.acquire()  # free again once the holder finished
    lock.release()


def test_killed_holder_leaves_a_stale_lock_that_is_taken_over(holder):
    path, start = holder
    _, pid = start()
    kill_and_wait(pid)  # like Task Scheduler ending a hung cycle
    lock = WorkerLock(path)
    for _ in range(100):  # Windows releases a dead process's locks asynchronously
        if lock.inspect().state != "held":
            break
        time.sleep(0.05)
    st = lock.inspect()
    assert st.state == "stale" and st.pid == pid and st.pid_alive is False
    assert lock.acquire()
    assert lock.taken_over["pid"] == pid
    lock.release()


def test_old_held_lock_is_flagged_suspect(holder):
    path, start = holder
    start()
    st = WorkerLock(path, stale_after=timedelta(seconds=-1)).inspect()
    assert st.state == "held" and st.suspect and "SUSPECT" in st.describe()


def test_pid_alive():
    assert pid_alive(os.getpid())
    assert not pid_alive(0) and not pid_alive(None)


def test_reading_the_holder_record_does_not_release_the_lock(holder):
    """Regression (Ubuntu, 2026-10-12): with POSIX record locks, opening and closing the lock file a
    second time inside the holder (which reading the holder record does) silently released the lock,
    so a second cycle could start. The holder script acquires (which reads the record) and we then read
    and inspect repeatedly from here: the lock must stay held throughout."""
    path, start = holder
    _, pid = start()
    for _ in range(3):
        assert path.read_bytes()  # another descriptor opened and closed in this process
        st = WorkerLock(path).inspect()
        assert st.state == "held" and st.pid == pid
        assert not WorkerLock(path).acquire()


def test_holder_can_inspect_itself_without_losing_the_lock(tmp_path):
    lock = WorkerLock(tmp_path / "worker.lock")
    assert lock.acquire()
    for _ in range(3):
        assert lock.inspect().state == "held"  # reads the record through a second descriptor
    other = WorkerLock(lock.path)
    assert not other.acquire() and other.inspect().state == "held"  # same process, another handle: still exclusive
    lock.release()
    assert other.acquire()
    other.release()


def test_an_ended_but_unreaped_process_is_not_alive():
    """A zombie (ended, parent has not waited yet) answers signal 0 on POSIX; it must not count as a
    running job or a live lock holder."""
    p = subprocess.Popen([sys.executable, "-c", "import os; print(os.getpid(), flush=True)"], stdout=subprocess.PIPE, text=True)
    pid = int(p.stdout.readline())
    try:
        for _ in range(200):  # not waited: on POSIX the child stays a zombie until p.wait()
            if not pid_alive(pid):
                break
            time.sleep(0.05)
        assert not pid_alive(pid)
    finally:
        p.wait(timeout=30)
