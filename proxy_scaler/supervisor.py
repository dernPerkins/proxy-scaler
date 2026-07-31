"""Process supervisor: manages Streamlit and the background worker as a
single managed unit.

Unlike db.py::ensure_worker_running()'s deliberately-detached worker spawn
(start_new_session=True on the worker alone, built to survive Streamlit's
dev-mode hot-reloads without losing an in-flight GPU job), this supervisor
explicitly owns *both* children's lifecycle: it starts them, health-checks
Streamlit, and on any shutdown trigger stops both — gracefully first
(SIGTERM/timeout), then forcefully (SIGKILL) if needed. That's the right
tradeoff for a headless server deployment or a packaged desktop app, where
there's no hot-reload churn and "stop the supervisor" should actually stop
everything.

Run via the `proxy-scaler-serve` console script, or
`python -m proxy_scaler.supervisor`. This is also what gets frozen (via
PyInstaller) into the desktop app's local-mode sidecar binary.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

from . import db

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8501
HEALTH_TIMEOUT_S = 60.0
HEALTH_POLL_INTERVAL_S = 0.5
SHUTDOWN_GRACE_S = 10.0
POLL_INTERVAL_S = 1.0

IS_WINDOWS = sys.platform == "win32"

ROOT = Path(__file__).resolve().parents[1]

# Printed to stdout once Streamlit is confirmed healthy — a stable marker
# any launcher (a human, a systemd unit, Tauri's sidecar stdout watcher)
# can watch for instead of guessing a fixed startup delay.
READY_MARKER = "PROXY_SCALER_READY"


class ShutdownRequested(Exception):
    """Raised from a signal handler to unwind the main wait loop cleanly."""


def _spawn(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start a child in its own session/process group. This isn't about
    detaching it (the supervisor still explicitly tracks and terminates it
    on shutdown — see _terminate below) — it gives the supervisor precise,
    individual control over each child via killpg/taskkill rather than
    relying on ambient signal propagation through a shared group."""
    kwargs: dict = {"cwd": str(ROOT)}
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
        kwargs["env"] = full_env
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _wait_for_health(host: str, port: int, timeout: float) -> bool:
    """Poll Streamlit's built-in health endpoint until it responds OK."""
    url = f"http://{host}:{port}/_stcore/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, timeout=2)
            if resp.ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(HEALTH_POLL_INTERVAL_S)
    return False


def _create_job_object():
    """Windows only: a Job Object with KILL_ON_JOB_CLOSE means the OS
    itself guarantees every assigned process dies when the job handle
    closes — including if this supervisor is killed forcefully (Task
    Manager "End Task", a crash, power loss), which plain child-tracking
    can't guarantee on Windows. Returns None if pywin32 isn't installed;
    callers fall back to the taskkill-based path in _terminate() below."""
    try:
        import win32job
    except ImportError:
        return None
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    info["BasicLimitInformation"]["LimitFlags"] = (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info
    )
    return job


def _assign_to_job(job, proc: subprocess.Popen) -> None:
    if job is None:
        return
    import win32con
    import win32job
    import win32api

    handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, proc.pid)
    win32job.AssignProcessToJobObject(job, handle)


def _terminate(proc: subprocess.Popen, *, grace_s: float = SHUTDOWN_GRACE_S) -> None:
    """Ask a child to stop, then insist.

    Unix: SIGTERM to its own process group (it was spawned via
    start_new_session, so its pid is also its pgid) — wait, SIGKILL
    fallback.

    Windows: plain terminate()/TerminateProcess gives the child no chance
    to clean up and is known to leave its own descendants behind (see
    plan notes — a real-world report of this exact architecture had to
    fall back to `taskkill` for reliable cleanup). Try CTRL_BREAK_EVENT
    first (only effective if the child installs a console-control
    handler — most plain Python children won't), then `taskkill /F /T` as
    the reliable fallback. A Job Object (see _create_job_object), when
    available, is the actual safety net for the "supervisor itself gets
    force-killed" case this function can't cover.
    """
    if proc.poll() is not None:
        return

    if IS_WINDOWS:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=grace_s)
            return
        except (subprocess.TimeoutExpired, OSError):
            pass
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            pass
        return

    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass


def main(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    db_path: Path | str | None = None,
    worker_lock_path: Path | str | None = None,
) -> int:
    """Start Streamlit + the worker as managed children, block until
    shutdown, then stop both cleanly. Returns a process exit code."""
    # Normally Streamlit's own startup (app.py::main -> db.init_db()) is
    # what creates the schema on a fresh DB path. Since the worker starts
    # concurrently here (not waiting on Streamlit), it can race ahead and
    # hit the DB before that's happened — exactly what a genuinely fresh
    # install (or an isolated test DB path) hits immediately, since
    # there's no pre-existing DB file to paper over the race. Initialize
    # it here, before spawning either child, so there's no race at all.
    db.init_db(db_path)

    worker_env: dict[str, str] = {}
    if db_path is not None:
        worker_env["PROXY_SCALER_DB_PATH"] = str(db_path)
    if worker_lock_path is not None:
        worker_env["PROXY_SCALER_WORKER_LOCK_PATH"] = str(worker_lock_path)

    print(f"Starting Streamlit on {host}:{port}…")
    streamlit_proc = _spawn(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(ROOT / "app.py"),
            "--server.address",
            host,
            "--server.port",
            str(port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ]
    )

    print("Starting worker…")
    worker_proc = _spawn(
        [sys.executable, "-u", "-m", "proxy_scaler.worker"],
        env=worker_env or None,
    )

    children = [streamlit_proc, worker_proc]
    job = _create_job_object() if IS_WINDOWS else None
    if job is not None:
        for proc in children:
            _assign_to_job(job, proc)

    def _handle_signal(signum, frame) -> None:  # noqa: ARG001
        raise ShutdownRequested()

    signal.signal(signal.SIGINT, _handle_signal)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, _handle_signal)

    exit_code = 0
    try:
        if _wait_for_health(host, port, HEALTH_TIMEOUT_S):
            print(READY_MARKER)
            sys.stdout.flush()
        else:
            print(
                "Streamlit did not become healthy in time; shutting down.",
                file=sys.stderr,
            )
            exit_code = 1
            raise ShutdownRequested()

        while True:
            for proc, name in ((streamlit_proc, "streamlit"), (worker_proc, "worker")):
                code = proc.poll()
                if code is not None:
                    print(
                        f"{name} exited unexpectedly (code {code}); shutting down.",
                        file=sys.stderr,
                    )
                    exit_code = code or 1
                    raise ShutdownRequested()
            time.sleep(POLL_INTERVAL_S)
    except ShutdownRequested:
        pass
    finally:
        print("Shutting down…")
        for proc in children:
            _terminate(proc)
        print("Shutdown complete.")

    return exit_code


def cli_main() -> int:
    """Entry point for both invocation paths — the `proxy-scaler-serve`
    console script (setuptools calls this directly, `__name__` is never
    "__main__" in that path) and `python -m proxy_scaler.supervisor`.
    Reads the same env var overrides (unset -> default) either way, so a
    test harness or launcher can run this as an isolated process without
    needing a CLI arg parser here — same convention as worker.py's own
    env var overrides."""
    return main(
        host=os.environ.get("PROXY_SCALER_SERVER_HOST", DEFAULT_HOST),
        port=int(os.environ.get("PROXY_SCALER_SERVER_PORT", DEFAULT_PORT)),
        db_path=os.environ.get("PROXY_SCALER_DB_PATH") or None,
        worker_lock_path=os.environ.get("PROXY_SCALER_WORKER_LOCK_PATH") or None,
    )


if __name__ == "__main__":
    sys.exit(cli_main())
