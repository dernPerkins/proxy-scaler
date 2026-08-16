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


class _FakeProc:
    """Stands in for the API child's Popen: poll() returns None while
    "running", then the exit code from `exits_after` polls onward."""

    def __init__(self, code: int | None, exits_after: int = 0) -> None:
        self._code = code
        self._polls_left = exits_after

    def poll(self) -> int | None:
        if self._polls_left > 0:
            self._polls_left -= 1
            return None
        return self._code


def test_wait_for_health_bails_out_when_child_already_exited(monkeypatch) -> None:
    """The bug this guards against: a second instance's uvicorn loses the
    port bind and exits in under a second, yet the supervisor sat out the
    full health timeout before reporting a generic failure. A dead child
    can never answer a probe, so the wait must end on the first poll —
    without a single HTTP request being made."""
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        raise requests.ConnectionError("never up")

    monkeypatch.setattr(supervisor.requests, "get", fake_get)

    start = time.monotonic()
    result = supervisor._wait_for_health(
        "127.0.0.1", 8501, timeout=30.0, proc=_FakeProc(code=1)
    )
    assert result is False
    assert calls["n"] == 0
    assert time.monotonic() - start < 5.0, "should not have waited out the timeout"


def test_wait_for_health_bails_out_when_child_exits_mid_wait(monkeypatch) -> None:
    def fake_get(url, timeout=None):
        raise requests.ConnectionError("never up")

    monkeypatch.setattr(supervisor.requests, "get", fake_get)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)

    start = time.monotonic()
    result = supervisor._wait_for_health(
        "127.0.0.1", 8501, timeout=30.0, proc=_FakeProc(code=1, exits_after=3)
    )
    assert result is False
    assert time.monotonic() - start < 5.0, "should not have waited out the timeout"


def test_wait_for_health_live_child_does_not_short_circuit(monkeypatch) -> None:
    """A running child (poll() → None) must leave the wait exactly as it
    was: still polling until the endpoint answers."""
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("not up yet")
        return _FakeResponse(ok=True)

    monkeypatch.setattr(supervisor.requests, "get", fake_get)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _s: None)

    result = supervisor._wait_for_health(
        "127.0.0.1", 8501, timeout=5.0, proc=_FakeProc(code=None)
    )
    assert result is True
    assert calls["n"] == 3


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
    port = supervisor.DEFAULT_PORT + (os.getpid() % 1000)  # avoid clashing with a real default-port instance
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


@pytest.mark.skipif(sys.platform != "win32", reason="CREATE_NO_WINDOW is Windows-only")
def test_supervisor_reaches_ready_without_a_console(tmp_path: Path) -> None:
    """Regression guard for a bug that shipped: with no console attached,
    the supervisor came up but its API server child never did.

    The desktop app is a GUI process, and Tauri's shell plugin spawns
    sidecars with CREATE_NO_WINDOW — so the supervisor runs with no
    console of its own. A console-subsystem child spawned from there
    *without* that same flag deadlocks during startup inside an LPC call
    to CSRSS while attaching to the console (three threads parked in
    EventPairLow, ~0.03s of CPU burned over a full minute). The uvicorn
    child hit it every time, so the API never bound, the supervisor's
    health check timed out, and the app surfaced only a generic "did not
    become ready" with no hint at the cause. See supervisor._spawn.

    Deliberately not part of the `running_supervisor` fixture's coverage:
    that fixture inherits this process's console, which is exactly the
    condition under which the bug does *not* reproduce. The whole point
    here is the console-less spawn, so this drives the subprocess itself.

    Asserts against the ready marker, a live HTTP response, and the
    worker's lock file rather than `ps`/`/proc` — all cross-platform, and
    a real HTTP response is stronger evidence than process existence
    anyway: the failure mode was a child that was alive but never bound.
    """
    port = supervisor.DEFAULT_PORT + (os.getpid() % 1000)
    lock_path = tmp_path / "worker.lock"

    env = dict(os.environ)
    env["PROXY_SCALER_SERVER_HOST"] = "127.0.0.1"
    env["PROXY_SCALER_SERVER_PORT"] = str(port)
    env["PROXY_SCALER_DB_PATH"] = str(tmp_path / "test.db")
    env["PROXY_SCALER_WORKER_LOCK_PATH"] = str(lock_path)

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "proxy_scaler.supervisor"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # The whole point of the test — mirrors how Tauri spawns this.
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    captured: list[str] = []

    def _fail(message: str) -> str:
        return f"{message}; supervisor output:\n" + "".join(captured)

    try:
        # Read line by line rather than communicate(): the supervisor runs
        # until stopped, so there's no EOF to wait for. Under the bug this
        # loop simply never sees the marker.
        ready = False
        deadline = time.monotonic() + 120.0  # torch cold start can be slow
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            line = proc.stdout.readline()
            if not line:
                break
            captured.append(line.decode("utf-8", "replace"))
            if supervisor.READY_MARKER in captured[-1]:
                ready = True
                break
        assert ready, _fail("supervisor never reported ready with no console attached")

        resp = requests.get(f"http://127.0.0.1:{port}/api/health", timeout=10)
        assert resp.ok, _fail(f"health endpoint returned {resp.status_code}")

        lock_deadline = time.monotonic() + 30.0
        while time.monotonic() < lock_deadline and not lock_path.exists():
            time.sleep(0.5)
        assert lock_path.exists(), _fail("worker never acquired its lock file")
    finally:
        # SIGTERM is a hard TerminateProcess on Windows, which would skip
        # the supervisor's own child cleanup — close stdin instead, the
        # same graceful trigger the desktop app uses.
        if proc.poll() is None:
            proc.stdin.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        proc.stdout.close()
