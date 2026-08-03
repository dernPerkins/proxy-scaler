"""Unit tests for db.py::default_data_dir() — the stable, OS-conventional
per-user data directory used (only when frozen) for DEFAULT_DB_PATH and,
via supervisor.py, as the frozen sidecar's working directory. Fixes a
real shipped bug: the previous __file__-derived default resolved inside
PyInstaller onefile's per-launch temp extraction directory, so saved
projects (and generated images, via supervisor.py's cwd) silently
vanished on every app restart."""

from __future__ import annotations

from pathlib import Path

from proxy_scaler import db


def test_default_data_dir_macos(monkeypatch) -> None:
    monkeypatch.setattr(db.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/test"))
    assert db.default_data_dir() == Path(
        "/Users/test/Library/Application Support/proxy-scaler"
    )


def test_default_data_dir_windows_uses_appdata(monkeypatch) -> None:
    monkeypatch.setattr(db.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "C:\\Users\\test\\AppData\\Roaming")
    assert db.default_data_dir() == Path("C:\\Users\\test\\AppData\\Roaming") / "proxy-scaler"


def test_default_data_dir_windows_falls_back_to_home(monkeypatch) -> None:
    monkeypatch.setattr(db.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("C:\\Users\\test"))
    assert db.default_data_dir() == Path("C:\\Users\\test") / "proxy-scaler"


def test_default_data_dir_linux_uses_xdg_data_home(monkeypatch) -> None:
    monkeypatch.setattr(db.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/home/test/.data")
    assert db.default_data_dir() == Path("/home/test/.data/proxy-scaler")


def test_default_data_dir_linux_falls_back_to_local_share(monkeypatch) -> None:
    monkeypatch.setattr(db.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/test"))
    assert db.default_data_dir() == Path("/home/test/.local/share/proxy-scaler")
