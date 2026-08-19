#!/usr/bin/env python3
"""Build/refresh dist/latest.json — the update manifest the desktop app
checks on boot (see desktop/src-tauri/src/update.rs) and the website's
download data.

    python packaging/generate-manifest.py [--notes TEXT] [--notes-url URL]

Scans dist/ for release artifacts matching the established filename
patterns, computes each one's size and sha256 (nobody should ever do that
by hand), and MERGES the entries into any dist/latest.json already there.
Merging is the point, not a convenience: releases are built in passes on
different machines (Linux box, the Mac, the Windows passes — see
docs/releasing.md), so each run may only see its own platform's artifacts
and must not throw away the others'. To accumulate across machines, seed
dist/latest.json with the copy from the previous machine (or from R2)
before running. The manifest lives in dist/ itself so the existing
`rclone copy dist/ ...` upload ships it alongside the artifacts.

Artifacts whose filename version doesn't match pyproject.toml's are
skipped with a warning (stale files from a previous release hanging
around in dist/), and kept entries recorded under a different version are
warned about too — the manifest must describe exactly one release.

Arch names are normalized to Rust's std::env::consts::ARCH vocabulary
(x86_64 / aarch64) so the client can match entries without a mapping
table; the URL keeps the artifact's literal filename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
MANIFEST = DIST / "latest.json"
SCHEMA = 1
DEFAULT_BASE_URL = "https://dl.proxy-scaler.com"
DEFAULT_NOTES_URL = "https://www.proxy-scaler.com/#download"

# filename pattern -> how to read app/platform/arch/variant out of it.
# Formats the in-app updater actually downloads are "setup-exe" and "dmg";
# the rest ("zip", "tar.gz", "deb") are included for completeness — the
# website's download page can be driven off this file too, and the client
# falls back to opening the download page when no installer-format entry
# matches it.
PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^proxy-scaler-(?P<app>client|server-app)_(?P<ver>[\d.]+)_windows-(?P<arch>x64)_(?P<variant>[a-z-]+)-setup\.exe$"
        ),
        "setup-exe",
    ),
    (
        re.compile(
            r"^proxy-scaler-(?P<app>client|server-app)_(?P<ver>[\d.]+)_windows-(?P<arch>x64)_(?P<variant>[a-z-]+)\.zip$"
        ),
        "zip",
    ),
    (
        re.compile(
            r"^proxy-scaler-(?P<app>client|server-app)_(?P<ver>[\d.]+)_macos-(?P<arch>arm64|x86_64)\.dmg$"
        ),
        "dmg",
    ),
    (
        re.compile(
            r"^proxy-scaler-(?P<app>client|server-app)_(?P<ver>[\d.]+)_linux-(?P<arch>[a-z0-9_]+)-(?P<variant>[a-z-]+)\.tar\.gz$"
        ),
        "tar.gz",
    ),
    # The headless server .deb (packaging/build-deb.sh) — app named
    # "server-deb" to keep it distinct from the desktop server-app.
    (
        re.compile(
            r"^proxy-scaler_(?P<ver>[\d.]+)_(?P<arch>[a-z0-9_]+)-(?P<variant>[a-z-]+)\.deb$"
        ),
        "deb",
    ),
]

ARCH_NORMALIZE = {"x64": "x86_64", "amd64": "x86_64", "arm64": "aarch64"}


def pkg_version() -> str:
    match = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    if not match:
        sys.exit("error: no version in pyproject.toml")
    return match.group(1)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def scan(version: str, base_url: str) -> list[dict]:
    entries = []
    for path in sorted(DIST.iterdir()):
        if not path.is_file() or path.name == MANIFEST.name or path.name.startswith("."):
            continue
        for pattern, fmt in PATTERNS:
            match = pattern.match(path.name)
            if not match:
                continue
            groups = match.groupdict()
            if groups["ver"] != version:
                print(f"  skipping {path.name}: version {groups['ver']} != {version} (stale?)")
                break
            print(f"  hashing {path.name} …")
            entries.append(
                {
                    "app": groups.get("app", "server-deb"),
                    "platform": "windows" if fmt in ("setup-exe", "zip")
                    else "macos" if fmt == "dmg"
                    else "linux",
                    "arch": ARCH_NORMALIZE.get(groups["arch"], groups["arch"]),
                    # macOS ships one build per arch, no GPU variants.
                    "variant": groups.get("variant") or "default",
                    "format": fmt,
                    "url": f"{base_url}/{path.name}",
                    "size": path.stat().st_size,
                    "sha256": sha256_of(path),
                }
            )
            break
    return entries


def entry_key(entry: dict) -> tuple:
    return (entry["app"], entry["platform"], entry["arch"], entry["variant"], entry["format"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--notes", help="release-notes text shown in the update prompt")
    parser.add_argument("--notes-url", help=f"link for full notes (default {DEFAULT_NOTES_URL})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="artifact host URL prefix")
    args = parser.parse_args()

    version = pkg_version()
    if not DIST.is_dir():
        sys.exit("error: no dist/ directory — build a release first")

    existing: dict = {}
    if MANIFEST.exists():
        existing = json.loads(MANIFEST.read_text())
        if existing.get("version") not in (None, version):
            print(
                f"warning: existing manifest is for {existing.get('version')}, "
                f"rewriting for {version} — entries for artifacts not present "
                "in this dist/ are dropped (rebuild or re-seed them)."
            )
            existing["artifacts"] = [
                e for e in existing.get("artifacts", []) if f"_{version}_" in e.get("url", "")
            ]

    fresh = scan(version, args.base_url.rstrip("/"))
    if not fresh and not existing.get("artifacts"):
        sys.exit("error: no release artifacts recognized in dist/ and no existing entries")

    merged: dict[tuple, dict] = {entry_key(e): e for e in existing.get("artifacts", [])}
    for entry in fresh:
        merged[entry_key(entry)] = entry

    manifest = {
        "schema": SCHEMA,
        "version": version,
        "released": date.today().isoformat(),
        "notes": args.notes if args.notes is not None else existing.get("notes", ""),
        "notes_url": args.notes_url or existing.get("notes_url") or DEFAULT_NOTES_URL,
        "artifacts": sorted(merged.values(), key=entry_key),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {MANIFEST.relative_to(ROOT)}: version {version}, {len(merged)} artifact(s)")
    print("upload together with the artifacts (rclone copy dist/ ...); upload latest.json LAST")


if __name__ == "__main__":
    main()
