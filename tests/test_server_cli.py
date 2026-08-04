"""Tests for the standalone-server surface of supervisor.py: flag parsing,
the flag-vs-env-var precedence the Tauri sidecar and the integration
fixture both depend on, and the stdin gating that makes headless
deployment (systemd, docker) possible at all."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from proxy_scaler import supervisor

ROOT = Path(__file__).resolve().parents[1]


class _RecordingMain:
    """Stands in for supervisor.main(), capturing the kwargs cli_main built."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def __call__(self, **kwargs) -> int:
        self.kwargs = kwargs
        return 0


@pytest.fixture
def recorded_main(monkeypatch) -> _RecordingMain:
    recorder = _RecordingMain()
    monkeypatch.setattr(supervisor, "main", recorder)
    # cli_main falls back to these when nothing else is set; clear them so
    # a developer's real environment can't leak into assertions.
    for var in (
        "PROXY_SCALER_SERVER_HOST",
        "PROXY_SCALER_SERVER_PORT",
        "PROXY_SCALER_DB_PATH",
        "PROXY_SCALER_WORKER_LOCK_PATH",
        "PROXY_SCALER_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return recorder


def test_defaults_bind_loopback_only(recorded_main) -> None:
    """The safe default matters: this server has no authentication, so it
    must not become network-reachable just because someone ran it."""
    supervisor.cli_main([])
    assert recorded_main.kwargs["host"] == supervisor.DEFAULT_HOST
    assert recorded_main.kwargs["host"] == "127.0.0.1"
    assert recorded_main.kwargs["port"] == supervisor.DEFAULT_PORT


def test_env_vars_still_drive_everything(recorded_main, monkeypatch) -> None:
    """Env vars predate the flags and are the only channel the Tauri
    sidecar and tests/test_supervisor.py's integration fixture use — they
    must keep working untouched."""
    monkeypatch.setenv("PROXY_SCALER_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("PROXY_SCALER_SERVER_PORT", "9123")
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", "/tmp/env.db")
    monkeypatch.setenv("PROXY_SCALER_WORKER_LOCK_PATH", "/tmp/env.lock")

    supervisor.cli_main([])

    assert recorded_main.kwargs["host"] == "0.0.0.0"
    assert recorded_main.kwargs["port"] == 9123
    assert recorded_main.kwargs["db_path"] == "/tmp/env.db"
    assert recorded_main.kwargs["worker_lock_path"] == "/tmp/env.lock"


def test_flags_win_over_env_vars(recorded_main, monkeypatch) -> None:
    monkeypatch.setenv("PROXY_SCALER_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("PROXY_SCALER_SERVER_PORT", "8000")

    supervisor.cli_main(["--host", "0.0.0.0", "--port", "9000"])

    assert recorded_main.kwargs["host"] == "0.0.0.0"
    assert recorded_main.kwargs["port"] == 9000


def test_data_dir_flag_derives_concrete_paths(recorded_main, tmp_path) -> None:
    """--data-dir has to produce explicit db/lock paths rather than lean on
    db.py's module constants: those are resolved at import time, which is
    already past by the time a command-line flag is parsed."""
    target = tmp_path / "srv"
    supervisor.cli_main(["--data-dir", str(target)])

    assert recorded_main.kwargs["db_path"] == str(target / "proxy_scaler.db")
    assert recorded_main.kwargs["worker_lock_path"] == str(target / "worker.lock")
    assert target.is_dir(), "the data dir should be created, not just named"
    # Exported so the API/worker children resolve their own defaults there.
    assert os.environ["PROXY_SCALER_DATA_DIR"] == str(target)


def test_explicit_db_path_beats_data_dir(recorded_main, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROXY_SCALER_DB_PATH", "/tmp/explicit.db")
    supervisor.cli_main(["--data-dir", str(tmp_path)])
    assert recorded_main.kwargs["db_path"] == "/tmp/explicit.db"
    # ...but the lock still comes from the data dir, since nothing set it.
    assert recorded_main.kwargs["worker_lock_path"] == str(tmp_path / "worker.lock")


def test_no_stdin_shutdown_flag_disables_the_watcher(recorded_main) -> None:
    supervisor.cli_main([])
    assert recorded_main.kwargs["watch_stdin"] is True

    supervisor.cli_main(["--no-stdin-shutdown"])
    assert recorded_main.kwargs["watch_stdin"] is False


@pytest.mark.skipif(os.name != "posix", reason="/dev/null detection is POSIX-only")
def test_devnull_stdin_is_not_treated_as_a_shutdown_trigger() -> None:
    """The whole reason headless deployment was broken: systemd and
    `docker run` (without -i) hand the process /dev/null, which reads as
    instant EOF — and EOF means "shut down". A service would stop itself
    the moment it finished starting."""
    with open(os.devnull) as devnull:
        original = sys.stdin
        sys.stdin = devnull
        try:
            assert supervisor._stdin_can_trigger_shutdown() is False
        finally:
            sys.stdin = original


def test_pipe_stdin_still_triggers_shutdown() -> None:
    """The Tauri sidecar's stdin is a real pipe, and the shutdown protocol
    depends on EOF there — gating for /dev/null must not break it."""
    read_fd, write_fd = os.pipe()
    original = sys.stdin
    try:
        sys.stdin = os.fdopen(read_fd)
        assert supervisor._stdin_can_trigger_shutdown() is True
    finally:
        sys.stdin.close()
        os.close(write_fd)
        sys.stdin = original


@pytest.mark.skipif(os.name != "posix", reason="uses /dev/null redirection")
def test_headless_launch_survives_devnull_stdin(tmp_path) -> None:
    """End-to-end version of the gating fix, without a service manager:
    launch the real supervisor with stdin=/dev/null (what systemd does)
    and confirm it's still alive a moment later instead of having shut
    itself down. Deliberately not waiting for full readiness — torch cold
    start is slow and the bug this covers fires immediately."""
    env = dict(os.environ)
    env["PROXY_SCALER_SERVER_HOST"] = "127.0.0.1"
    env["PROXY_SCALER_SERVER_PORT"] = str(8000 + (os.getpid() % 1000) + 1)
    env["PROXY_SCALER_DB_PATH"] = str(tmp_path / "test.db")
    env["PROXY_SCALER_WORKER_LOCK_PATH"] = str(tmp_path / "worker.lock")

    with open(os.devnull) as devnull:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "proxy_scaler.supervisor"],
            cwd=str(ROOT),
            env=env,
            stdin=devnull,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
