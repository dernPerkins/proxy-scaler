"""Tests for supervisor.py: health-check polling, and a real end-to-end
lifecycle check (spawn the actual supervisor subprocess, confirm its
Streamlit + worker children come up, signal it, confirm both children are
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


def _child_pids(ppid: int, needle: str) -> list[int]:
    """PIDs of *direct children* of `ppid` whose command line contains
    `needle`. Filtering by parent PID (not just command line) is what
    keeps this reliable even when an unrelated real worker/Streamlit
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
    entirely and orphans Streamlit/worker underneath it."""
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


@pytest.mark.skipif(sys.platform == "win32", reason="ps-based check is Linux/Unix-only")
def test_supervisor_spawns_and_cleanly_stops_both_children(tmp_path: Path) -> None:
    """End-to-end: run the real supervisor as a subprocess (isolated
    port/DB/lock so it can't collide with anything else running), confirm
    both its Streamlit and worker children actually start, send it a
    shutdown signal, and confirm both children are actually gone
    afterward — the real regression this phase is about (today's detached
    worker survives its parent; the supervisor's children must not)."""
    port = 8000 + (os.getpid() % 1000)  # avoid clashing with a real 8501 instance
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
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        ready = False
        deadline = time.monotonic() + 90.0  # Streamlit/torch cold start can be slow
        while time.monotonic() < deadline:
            if supervisor.READY_MARKER in log_path.read_text():
                ready = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        assert ready, f"supervisor never reported ready; output so far:\n{log_path.read_text()}"

        streamlit_pids = _child_pids(proc.pid, "streamlit")
        worker_pids = _child_pids(proc.pid, "proxy_scaler.worker")
        assert streamlit_pids, (
            f"no Streamlit child found under our test supervisor; log:\n{log_path.read_text()}"
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

        # Ask the supervisor to shut down, same as Ctrl+C / a service
        # manager stopping it.
        _shutdown(proc)

        # The regression this phase exists to fix: both children must
        # actually be gone, not orphaned.
        for pid in streamlit_pids:
            assert not Path(f"/proc/{pid}").exists(), "Streamlit child survived shutdown"
        for pid in worker_pids:
            assert not Path(f"/proc/{pid}").exists(), "worker child survived shutdown"
    finally:
        _shutdown(proc)
        log_file.close()
