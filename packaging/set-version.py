#!/usr/bin/env python3
"""Keep the version in lockstep across every file that carries one.

    python packaging/set-version.py 0.2.0    rewrite all files to 0.2.0
    python packaging/set-version.py --check  verify they all agree (exit 1 on drift)

pyproject.toml is the canonical copy — the Makefile's PKG_VERSION and
build-deb.sh both sed it out for artifact naming — but six other files
carry their own: the two tauri.conf.json files (what the MSI/.app bundles
report as the installed version), the two Cargo.toml files (what
env!("CARGO_PKG_VERSION") bakes into the binaries for the update check),
proxy_scaler/__init__.py (what /api/version reports at runtime, frozen
builds included), and the frontend package.json. Nothing else enforces
agreement, which is exactly how a release once shipped installers
reporting a different version than the API. `make check-version` gates
the release targets on this script's --check mode.

Plain regex on the first match rather than real TOML/JSON parsing:
every one of these files keeps its version near the top and a
parse-and-rewrite would reformat the whole file just to change one line.
Cargo.lock is deliberately not touched — cargo heals it on the next
build.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (path, pattern) — pattern's first group is everything before the version,
# second group everything after; replacement is group1 + version + group2.
# Only the FIRST match in each file is rewritten/read: dependency tables
# further down (e.g. Cargo.toml's `tauri = { version = "2" }`) also match
# the word "version" and must be left alone.
FILES: list[tuple[str, str]] = [
    ("pyproject.toml", r'(^version = ")([^"]+)(")'),
    ("proxy_scaler/__init__.py", r'(^__version__ = ")([^"]+)(")'),
    ("desktop/src-tauri/tauri.conf.json", r'(^  "version": ")([^"]+)(")'),
    ("desktop/server-app/tauri.conf.json", r'(^  "version": ")([^"]+)(")'),
    ("desktop/src-tauri/Cargo.toml", r'(^version = ")([^"]+)(")'),
    ("desktop/server-app/Cargo.toml", r'(^version = ")([^"]+)(")'),
    ("desktop/frontend/package.json", r'(^  "version": ")([^"]+)(")'),
]

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read_version(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(), flags=re.MULTILINE)
    if not match:
        sys.exit(f"error: no version line matching {pattern!r} in {path}")
    return match.group(2)


def check() -> None:
    versions = {rel: read_version(ROOT / rel, pat) for rel, pat in FILES}
    if len(set(versions.values())) == 1:
        print(f"version OK: {next(iter(versions.values()))} everywhere")
        return
    print("error: version drift detected:", file=sys.stderr)
    for rel, ver in versions.items():
        print(f"  {ver:12} {rel}", file=sys.stderr)
    print("fix with: make set-version VERSION=x.y.z", file=sys.stderr)
    sys.exit(1)


def set_version(new: str) -> None:
    if not VERSION_RE.match(new):
        sys.exit(f"error: {new!r} is not a plain x.y.z version")
    for rel, pat in FILES:
        path = ROOT / rel
        old = read_version(path, pat)
        text = re.sub(pat, lambda m: m.group(1) + new + m.group(3), path.read_text(), count=1, flags=re.MULTILINE)
        path.write_text(text)
        marker = "" if old != new else " (unchanged)"
        print(f"  {old} -> {new}  {rel}{marker}")
    print(
        "done. Cargo.lock updates itself on the next cargo build; "
        "commit these together."
    )


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().split("\n\n")[0])
    if sys.argv[1] == "--check":
        check()
    else:
        set_version(sys.argv[1])


if __name__ == "__main__":
    main()
