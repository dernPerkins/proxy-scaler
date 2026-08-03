"""Tests for supervisor.py: health-check polling, and a real end-to-end
lifecycle check (spawn the actual supervisor subprocess, confirm its
API server + worker children come up, signal it, confirm both children are
actually gone afterward — not just that the signal was sent)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from proxy_scaler import supervisor

ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    def __init__(self, *, ok: bool) -> None:
        self.ok = ok


def test_wait_for_health_returns_true_once_healthy(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("not up yet")
        return _FakeResponse(ok=True)

    monkeypatch.setattr(supervisor.requests, "get", fake_get)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)

    assert supervisor._wait_for_health("127.0.0.1", 8501, timeout=5.0) is True
    assert calls["n"] == 3


def test_wait_for_health_times_out(monkeypatch) -> None:
    def fake_get(url, timeout=None):
        raise requests.ConnectionError("never up")

    monkeypatch.setattr(supervisor.requests, "get", fake_get)

    # Real, tiny timeout — no time.sleep monkeypatch needed since this
    # should genuinely take well under a second.
    assert supervisor._wait_for_health("127.0.0.1", 8501, timeout=0.2) is False


def test_child_command_not_frozen_uses_module_flag(monkeypatch) -> None:
    """Regression guard for a real shipped bug: outside a frozen build,
    sys.executable is a genuine Python interpreter, so children are
    launched via `-m` as before."""
    monkeypatch.setattr(supervisor, "IS_FROZEN", False)
    assert supervisor._child_command("worker") == [
        sys.executable,
        "-u",
        "-m",
        "proxy_scaler.worker",
    ]
    assert supervisor._child_command("api", ["--host", "127.0.0.1", "--port", "8000"]) == [
        sys.executable,
        "-m",
        "uvicorn",
        "proxy_scaler.api:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]


def test_child_command_frozen_uses_role_flag_not_module_flag(monkeypatch) -> None:
    """The actual bug this guards against: inside a PyInstaller-frozen
    build, sys.executable IS the frozen binary, not a Python interpreter.
    `[sys.executable, "-m", "uvicorn", ...]` doesn't invoke uvicorn at
    all in that case — it just re-runs this program's own entry point
    again, ignoring the args, which then does the same thing for its own
    children: an uncontrolled self-spawning loop. Frozen builds must never
    produce a `-m`-flavored command; they must always route through the
    --role dispatch in frozen_main() instead."""
    monkeypatch.setattr(supervisor, "IS_FROZEN", True)
    worker_cmd = supervisor._child_command("worker")
    api_cmd = supervisor._child_command("api", ["--host", "127.0.0.1", "--port", "8000"])

    assert worker_cmd == [sys.executable, "--role", "worker"]
    assert api_cmd == [
        sys.executable,
        "--role",
        "api",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    assert "-m" not in worker_cmd
    assert "-m" not in api_cmd


def _child_pids(ppid: int, needle: str) -> list[int]:
    """PIDs of *direct children* of `ppid` whose command line contains
    `needle`. Filtering by parent PID (not just command line) is what
    keeps this reliable even when an unrelated real worker/API-server
    process happens to already be running in the same environment —
    setsid() (used to give each child its own process group, see
    supervisor._spawn) does not change its parent PID, so this still
    correctly isolates only the processes our own test supervisor
    spawned. Linux/Unix-only (reads `ps`).

    --cols is required: `ps`'s args column truncates to a detected
    terminal width, which defaults to something narrow with no
    controlling terminal (e.g. invoked from deep inside a pytest
    subprocess) — narrow enough to silently cut off a trailing
    module name like "proxy_scaler.worker" and make a live process
    look like it doesn't exist. Confirmed by direct reproduction.
    """
    out = subprocess.run(
        ["ps", "-eo", "pid,ppid,args", "--cols", "2000"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pids = []
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, ppid_str, args = parts
        if ppid_str == str(ppid) and needle in args:
            try:
                pids.append(int(pid_str))
            except ValueError:
                pass
    return pids


def _shutdown(proc: subprocess.Popen) -> None:
    """Graceful-then-forceful stop of a supervisor subprocess we spawned
    for a test. Never just proc.kill() as the only attempt — SIGKILL
    can't be caught, so it skips the supervisor's own child-cleanup
    entirely and orphans the API server/worker underneath it."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=30)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


class _RunningSupervisor:
    def __init__(
        self,
        proc: subprocess.Popen,
        log_path: Path,
        api_pids: list[int],
        worker_pids: list[int],
    ) -> None:
        self.proc = proc
        self.log_path = log_path
        self.api_pids = api_pids
        self.worker_pids = worker_pids

    def assert_children_gone(self) -> None:
        for pid in self.api_pids:
            assert not Path(f"/proc/{pid}").exists(), "API server child survived shutdown"
        for pid in self.worker_pids:
            assert not Path(f"/proc/{pid}").exists(), "worker child survived shutdown"


@pytest.fixture
def running_supervisor(tmp_path: Path):
    """Spawn the real supervisor as a subprocess (isolated port/DB/lock so
    it can't collide with anything else running), wait for it to report
    ready, and confirm both its API server and worker children actually
    started. Yields a _RunningSupervisor; always cleans up via SIGTERM
    even if the test already stopped it a different way (double-stop is a
    no-op — see _shutdown's poll() check)."""
    port = 8000 + (os.getpid() % 1000)  # avoid clashing with a real default-port instance
    db_path = tmp_path / "test.db"
    lock_path = tmp_path / "worker.lock"
    log_path = tmp_path / "supervisor.log"

    env = dict(os.environ)
    env["PROXY_SCALER_SERVER_HOST"] = "127.0.0.1"
    env["PROXY_SCALER_SERVER_PORT"] = str(port)
    env["PROXY_SCALER_DB_PATH"] = str(db_path)
    env["PROXY_SCALER_WORKER_LOCK_PATH"] = str(lock_path)

    log_file = log_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "proxy_scaler.supervisor"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,  # must be an open pipe, not inherited — an
        # inherited stdin that's already closed/EOF in this environment
        # would trigger an immediate shutdown via the stdin-watcher most
        # tests using this fixture aren't trying to exercise.
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        ready = False
        deadline = time.monotonic() + 90.0  # torch cold start (worker) can be slow
        while time.monotonic() < deadline:
            if supervisor.READY_MARKER in log_path.read_text():
                ready = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        assert ready, f"supervisor never reported ready; output so far:\n{log_path.read_text()}"

        api_pids = _child_pids(proc.pid, "uvicorn")
        worker_pids = _child_pids(proc.pid, "proxy_scaler.worker")
        assert api_pids, (
            f"no API server child found under our test supervisor; log:\n{log_path.read_text()}"
        )
        assert worker_pids, (
            f"no worker child found under our test supervisor; log:\n{log_path.read_text()}"
        )

        # The worker process existing (just confirmed above) doesn't mean
        # it's finished starting up yet — it can still be mid-import
        # (pipeline.py pulls in torch) before it reaches
        # acquire_worker_lock() in its own main(). Poll rather than a
        # single snapshot.
        lock_deadline = time.monotonic() + 30.0
        while time.monotonic() < lock_deadline and not lock_path.exists():
            time.sleep(0.5)
        assert lock_path.exists(), (
            f"worker never acquired its (isolated) lock file; log:\n{log_path.read_text()}"
        )

        yield _RunningSupervisor(proc, log_path, api_pids, worker_pids)
    finally:
        _shutdown(proc)
        log_file.close()


@pytest.mark.skipif(sys.platform == "win32", reason="ps-based check is Linux/Unix-only")
def test_supervisor_spawns_and_cleanly_stops_both_children(running_supervisor) -> None:
    """The core regression this phase is about: today's detached worker
    survives its parent; the supervisor's children must not."""
    _shutdown(running_supervisor.proc)  # same as Ctrl+C / a service manager stopping it
    running_supervisor.assert_children_gone()


@pytest.mark.skipif(sys.platform == "win32", reason="ps-based check is Linux/Unix-only")
def test_supervisor_stops_on_stdin_close(running_supervisor) -> None:
    """Tauri's sidecar API documents CommandChild::kill() as a hard kill,
    not a graceful signal — closing the sidecar's stdin pipe is the
    documented workaround for triggering a clean shutdown instead. Confirm
    that path alone (no SIGTERM at all) still cleanly stops both
    children."""
    running_supervisor.proc.stdin.close()
    running_supervisor.proc.wait(timeout=30)
    running_supervisor.assert_children_gone()
