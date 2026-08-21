#!/usr/bin/env python3
"""Build/refresh AND SIGN dist/latest.json — the update manifest the
desktop app checks on boot (see desktop/src-tauri/src/update.rs) and the
website's download data.

    python packaging/generate-manifest.py [--notes TEXT] [--notes-url URL]

The manifest names each installer's URL and sha256, so the manifest
itself is the thing that must be authentic: this script signs it with
minisign (dist/latest.json.minisig, uploaded alongside), and the apps
refuse any manifest that doesn't verify against their compiled-in public
key. The secret key lives ONLY on release machines (default
~/.minisign/minisign.key, override with --signing-key or
PROXY_SCALER_SIGNING_KEY) and is never committed — see docs/releasing.md
for generation, backup, and rotation. Before signing, the public halves
embedded in desktop/src-tauri/src/update.rs and
desktop/server-app/src/main.rs are checked against each other, and the
fresh signature is verified against them — a release signed with a key
the shipped apps don't trust never leaves this script. --skip-sign
exists for intermediate multi-machine passes; the final pass before
upload must sign.

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
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
MANIFEST = DIST / "latest.json"
SIGNATURE = DIST / "latest.json.minisig"
# The two compiled-in copies of the verification key. Kept in Rust consts
# (not read from a file at runtime) so nothing on a user's disk can swap
# the key out from under a shipped build.
PUBKEY_SOURCES = [
    ROOT / "desktop" / "src-tauri" / "src" / "update.rs",
    ROOT / "desktop" / "server-app" / "src" / "main.rs",
]
DEFAULT_SIGNING_KEY = os.environ.get(
    "PROXY_SCALER_SIGNING_KEY", str(Path.home() / ".minisign" / "minisign.key")
)
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


def embedded_pubkey(path: Path) -> str:
    match = re.search(r'^const MANIFEST_PUBKEY: &str = "([^"]+)";', path.read_text(), re.M)
    if not match:
        sys.exit(f"error: no MANIFEST_PUBKEY constant found in {path.relative_to(ROOT)}")
    return match.group(1)


def sign_manifest(secret_key: Path, version: str) -> None:
    keys = {source: embedded_pubkey(source) for source in PUBKEY_SOURCES}
    unique = set(keys.values())
    if len(unique) != 1:
        detail = ", ".join(f"{p.relative_to(ROOT)}={k[:16]}…" for p, k in keys.items())
        sys.exit(
            "error: the MANIFEST_PUBKEY constants disagree — paste the same "
            f".pub line into every copy ({detail})"
        )
    pubkey = unique.pop()
    if pubkey == "UNSET":
        sys.exit(
            "error: MANIFEST_PUBKEY is still the UNSET placeholder. One-time "
            "setup: `minisign -G`, paste the public key line into "
            "desktop/src-tauri/src/update.rs and desktop/server-app/src/main.rs, "
            "rebuild, then re-run. See docs/releasing.md."
        )
    if shutil.which("minisign") is None:
        sys.exit("error: minisign not found — apt install minisign / brew install minisign")
    if not secret_key.exists():
        sys.exit(
            f"error: signing key {secret_key} not found "
            "(--signing-key or PROXY_SCALER_SIGNING_KEY points at it)"
        )
    # Interactive on purpose: minisign prompts for the key's password.
    try:
        subprocess.run(
            [
                "minisign", "-S", "-s", str(secret_key),
                "-m", str(MANIFEST), "-x", str(SIGNATURE),
                "-t", f"proxy-scaler latest.json {version}",
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit("error: minisign signing failed — manifest is NOT signed, do not upload")
    # End-to-end: the signature just written must verify against the key
    # compiled into the apps, or every shipped build would refuse this
    # release. Catches signing with the wrong key file.
    try:
        subprocess.run(
            ["minisign", "-V", "-P", pubkey, "-m", str(MANIFEST), "-x", str(SIGNATURE)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        SIGNATURE.unlink(missing_ok=True)
        sys.exit(
            "error: the fresh signature does NOT verify against the apps' "
            "embedded MANIFEST_PUBKEY — wrong signing key? Signature removed."
        )
    print(f"signed: {SIGNATURE.relative_to(ROOT)} (verified against the embedded public key)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--notes", help="release-notes text shown in the update prompt")
    parser.add_argument("--notes-url", help=f"link for full notes (default {DEFAULT_NOTES_URL})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="artifact host URL prefix")
    parser.add_argument(
        "--signing-key",
        default=DEFAULT_SIGNING_KEY,
        help="minisign secret key to sign with (default: $PROXY_SCALER_SIGNING_KEY "
        "or ~/.minisign/minisign.key; never in the repo)",
    )
    parser.add_argument(
        "--skip-sign",
        action="store_true",
        help="write the manifest without signing — intermediate multi-machine "
        "passes only; shipped apps REFUSE an unsigned manifest",
    )
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

    if args.skip_sign:
        # A stale signature over the previous contents must not ride
        # along — it wouldn't verify, but its presence would look signed.
        if SIGNATURE.exists():
            SIGNATURE.unlink()
            print(f"removed stale {SIGNATURE.relative_to(ROOT)}")
        print(
            "WARNING: manifest left UNSIGNED (--skip-sign) — shipped apps will "
            "refuse it; run the final pass without --skip-sign before uploading."
        )
    else:
        sign_manifest(Path(args.signing_key).expanduser(), version)

    print(
        "upload together with the artifacts (rclone copy dist/ ...); upload "
        "latest.json + latest.json.minisig LAST, signature before manifest"
    )


if __name__ == "__main__":
    main()
