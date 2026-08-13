# Releasing

How to build the shipped artifacts, per OS. Read the [rules that apply
everywhere](#rules-that-apply-everywhere) first — most of the sharp edges
here come from those rather than from any one platform.

## Rules that apply everywhere

**You must build on the OS you're targeting.** There is no
cross-compiling. The generation server ships as a PyInstaller *onedir*
bundle (`proxy-scaler-serve/`) that embeds a platform-specific Python
runtime plus the whole torch/CUDA dependency tree. A Linux `.deb` cannot
be produced from macOS, a `.dmg` cannot be produced from Linux, and so
on. `make release` and `make deb` refuse outright to run off-Linux rather
than failing several GB into the work.

**The sidecar is the expensive step, and it's the one that goes stale.**
`make sidecar` re-freezes torch every time (several minutes, ~4.4GB out).
It's only needed when Python (`proxy_scaler/*`) changed — a Rust- or
frontend-only change doesn't need it. But if Python *did* change and you
skip it, every artifact you build silently packages the old server.

**Every app finds its sidecar as a sibling of its own executable**, via
`std::env::current_exe()` — not through Tauri's `resource_dir()` API.
See the comment at the top of `desktop/src-tauri/src/main.rs` for the
full reasoning. The practical consequence is that *placing* the sidecar
correctly is a per-platform concern, and it's where every packaging bug
in this project has come from so far.

**Artifacts are ~3.5–4.2GB each.** That's over GitHub's hard 2GB
per-file release-asset limit, so releases are distributed via Google
Drive rather than attached to a GitHub release. Splitting into <2GB
chunks would also work but hasn't been needed.

## GPU variants

`torch` builds for different GPU vendors are mutually exclusive — ROCm
and DirectML *replace* the default CUDA-capable wheel rather than adding
to it. So a GPU variant is chosen at install time, and produces a
separate artifact:

```bash
make install                          # default: CUDA-capable (also fine CPU-only)
GPU_VARIANT=rocm make install         # AMD on Linux
GPU_VARIANT=directml make install     # AMD on Windows
```

Then `make sidecar` freezes whatever is in the venv. Serving both Nvidia
and AMD users on the same OS means two full build passes with different
`GPU_VARIANT` values, and artifact filenames that distinguish them.

| Vendor | Platform | Mechanism |
|---|---|---|
| Nvidia | any | CUDA (default install) |
| Apple | macOS | Metal / MPS (built in) |
| AMD | Linux | ROCm — reports through the same `torch.cuda.*` APIs, so no code path of its own |
| AMD | Windows | DirectML — a genuinely separate backend (`privateuseone`), handled in `upscale.py::resolve_device` |

AMD-on-Windows via DirectML pins `torch==2.4.1`, which has no wheels for
the newest Python releases — see [Windows](#windows) for the interpreter
pin that needs.

---

## Linux

Produces all three Linux artifacts in one command.

```bash
make install          # once, or after dependency changes
make release
```

Outputs to `dist/`:

| Artifact | What it is |
|---|---|
| `proxy-scaler-client_<version>_linux-<arch>.tar.gz` | Desktop app + its sidecar |
| `proxy-scaler-server-app_<version>_linux-<arch>.tar.gz` | Tray/status-window server + its sidecar |
| `proxy-scaler_<version>_<arch>.deb` | Headless server, installs to `/opt/proxy-scaler` with an opt-in systemd unit |

`release` runs `sidecar-release` → client archive → server-app archive →
`deb`, in that order. It is **not** safe under `make -j`: it relies on
plain left-to-right prerequisite ordering.

Linux desktop artifacts ship as `.tar.gz`, not as Tauri-bundled
`.deb`/`.AppImage` installers. Tauri's Linux bundles install the binary
to `/usr/bin` and resources to `/usr/lib/<name>` — not siblings, which
the `current_exe()`-relative sidecar lookup requires. The hand-rolled
`packaging/build-deb.sh` (headless server only, `/opt/proxy-scaler`)
sidesteps that entirely and is unrelated to Tauri's bundler.

### Long builds over SSH

A full release takes long enough that an SSH drop can kill it. Run it
detached instead:

```bash
make release-bg       # returns immediately; safe to close the session
make release-status   # progress, or exit code once finished
```

`release-bg` logs to `dist/.release.log` and writes its exit code to
`dist/.release.exit`.

### Notes

- The `.deb` stages into `${TMPDIR:-/tmp}/proxy-scaler-deb-stage`, *not*
  inside the repo. `dpkg-deb` refuses to build from a directory looser
  than 0775, and a repo checkout on a FUSE/NTFS mount reports 0777 no
  matter what you `chmod` it to. Override with `PROXY_SCALER_DEB_STAGE`.
- Sidecar staging tries hardlinks (`cp -al`) first and falls back to a
  dereferencing copy. On filesystems that refuse hardlinks you'll see a
  wall of `cp: cannot create hard link ... Operation not permitted`
  followed by the build carrying on normally — that's the fallback
  working, not a failure.

---

## macOS

**Use `make macos-release`. Do not use a bare `cargo tauri build`.**

```bash
make install          # once
make sidecar-release  # if Python changed since the last freeze
make macos-release
```

Outputs `dist/proxy-scaler-{client,server-app}_<version>_macos-<arch>.dmg`.
Build one at a time with `macos-release-client` / `macos-release-server-app`.

### Why the dedicated target exists

`cargo tauri build` assembles the `.app` and then builds its `.dmg` from
it, all in one step. The sidecar can only be copied in *after* the `.app`
exists — by which point Tauri's `.dmg` has already been sealed, from the
unpatched app. That `.dmg` installs an app with no `proxy-scaler-serve/`
directory at all, and the client fails at spawn with "Couldn't start the
local server."

This shipped exactly once. `macos-release` prevents it by building
`--bundles app` only, patching the sidecar in, re-signing the bundle,
then producing the `.dmg` itself with `hdiutil` — Tauri's own dmg step is
never used.

### Where the sidecar goes inside the .app

`Contents/Resources/proxy-scaler-serve/` — **not** `Contents/MacOS/`
next to the binary, which is where every other platform puts it.

Apple's bundle format defines `Contents/MacOS/` as executables-only, and
`codesign` enforces it. Signing a bundle with a PyInstaller onedir tree
in there fails outright on the first non-code file it meets:

```
Proxy Scaler.app: code object is not signed at all
In subcomponent: .../proxy-scaler-serve/_internal/pyphen/dictionaries/hyph_lt.dic
```

That is not fixable by signing harder — a hyphenation dictionary is not
code, and `Contents/MacOS` has no way to say so. `Contents/Resources/` is
where non-code belongs; its contents are sealed by hash instead of being
treated as nested code.

`main.rs` therefore checks two locations and takes whichever exists: a
sibling of the binary (dev builds, and the shipped Windows/Linux layout),
then `../Resources/`. One lookup, correct everywhere, no
`cfg!(target_os)`. If neither exists the error names both paths it tried.

`bundle.resources` is still deliberately *not* configured for macOS —
Tauri would copy the tree a second time during the build.

### Always verify before distributing

```bash
hdiutil attach dist/proxy-scaler-client_0.1.0_macos-$(uname -m).dmg \
  -nobrowse -mountpoint /tmp/psdmg
ls /tmp/psdmg                                     # app + Applications symlink
ls /tmp/psdmg/"Proxy Scaler.app"/Contents/Resources/  # sidecar dir
codesign --verify --strict /tmp/psdmg/"Proxy Scaler.app"
hdiutil detach /tmp/psdmg
```

You want **both** `proxy-scaler-spike` and a `proxy-scaler-serve`
**directory**. If the directory is missing, the build is broken — don't
ship it. The volume itself should contain the `.app` *and* an
`Applications` symlink, and the signature check should print nothing (it
only speaks up on failure).

### What the .dmg contains

A window with the `.app` and a symlink to `/Applications` next to it, so
installing is the usual drag-across. `hdiutil create -srcfolder` is
pointed at a staging directory holding both (built and then deleted under
`desktop/*/target/release/dmg-stage`), never straight at the `.app` —
that only ever produces a volume containing a lone app icon with nowhere
to drop it.

There is no custom window size, background image or icon positioning.
That part lives in a `.DS_Store` that has to be produced by driving
Finder over AppleScript during the build: it needs a logged-in GUI
session, breaks in CI, and buys nothing functional. Deliberately skipped.

### Code signing

**Ad-hoc only.** `macos-bundle-{client,server-app}-sidecar` runs
`codesign --force --sign -` on the `.app` immediately after the
sidecar is copied in, and verifies it. This is not a substitute for
notarization — there's still no Developer ID, so Gatekeeper still warns
on first launch of a downloaded build (right-click → Open). Proper
notarization needs a paid Apple Developer account.

Why it's needed at all: `cargo tauri build` signs the bundle it just
produced, and copying a multi-GB directory into `Contents/Resources/`
afterwards invalidates that signature — bundle contents are sealed
under the signature's resource rules, so its contents are sealed rather
than ignored. A bundle with a *broken* signature is worse than an
unsigned one: Gatekeeper is stricter about it, and LaunchServices may
decline to register the app, which is one plausible reason a freshly
installed build never showed up in Spotlight (LaunchServices discovers
apps through Spotlight's index, so the two failures look identical from
the outside).

**Do not add `--deep`.** It was tried and it fails outright:

```
Proxy Scaler.app: bundle format unrecognized, invalid, or unsuitable
In subcomponent: .../proxy-scaler-serve/_internal/websockets-16.1.1.dist-info
```

`--deep` decides what counts as a nested *bundle* partly by directory
name, and treats dotted names as `.framework`/`.bundle`-shaped. Every
Python package in PyInstaller's `_internal/` ships a
`<pkg>-<version>.dist-info` directory, so `--deep` walks into one, finds
no `Contents/Info.plist`, and aborts. That's `--deep` misreading Python
packaging metadata, not a signing problem to solve.

Signing the outer bundle alone is also the correct scope, not merely the
one that works: PyInstaller ad-hoc signs the binaries it emits on macOS
(it must — arm64 refuses to execute unsigned code), so the sidecar's own
Mach-O files arrive already signed. The sidecar copy only broke the
*outer* seal, which is precisely what this restores. Apple has deprecated
`--deep` for signing anyway (macOS 13+), in favour of inside-out signing.

If the outer-only signature ever proves insufficient, the replacement is
an inside-out pass — sign the sidecar's Mach-O files individually, then
the outer `.app` last, still without `--deep`:

```bash
find "<app>/Contents/Resources/proxy-scaler-serve" -type f -print0 |
  while IFS= read -r -d '' f; do
    file -b "$f" | grep -q Mach-O && codesign --force --sign - "$f"
  done
codesign --force --sign - "<app>"
```

That's slow (a `file` call per entry across ~10k files) which is why it
isn't the default.

### Spotlight

Tauri's generated `Info.plist` already carries everything Spotlight and
LaunchServices need (`CFBundleIdentifier`, `CFBundleName`,
`CFBundleDisplayName`, `CFBundleExecutable`, `CFBundlePackageType`), from
`productName`/`identifier` in each `tauri.conf.json` — nothing to add
there.

If a previously installed (broken-signature) build is already on the
machine, macOS caches its registration, so a fixed build may still not
appear. Force the re-index after installing:

```bash
# 1. Confirm the installed app's signature is actually valid now
codesign --verify --strict --verbose=2 "/Applications/Proxy Scaler.app"

# 2. Re-register it with LaunchServices
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "/Applications/Proxy Scaler.app"

# 3. Re-index it for Spotlight
mdimport "/Applications/Proxy Scaler.app"

# 4. Check it took — should print the app's metadata, not nothing
mdls "/Applications/Proxy Scaler.app" | head

# Bigger hammers, in escalating order, if it still doesn't show up:
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -kill -r -domain local -domain system -domain user
sudo mdutil -E /            # full re-index of the boot volume (slow)
```

Also worth ruling out on the user's side: System Settings → Spotlight,
where both "Applications" as a result category and any Privacy exclusion
covering `/Applications` will suppress the app regardless of how it's
signed.

---

## Windows

Run from **Git Bash or MSYS2**, not `cmd`/PowerShell — the Makefile
detects those environments (`MINGW*`/`MSYS*`/`CYGWIN*`) to find the
venv's `Scripts/` directory and `.exe` suffixes.

```bash
make install                 # or: GPU_VARIANT=directml make install PY="py -3.12"
make sidecar
cd desktop/src-tauri && cargo tauri build      # client MSI
cd ../server-app && cargo tauri build          # server app MSI
```

Outputs land in each project's `target/release/bundle/msi/`.

### Interpreter pin

`make install` bootstraps the venv with the `py` launcher rather than
`python3` — on Windows that name is usually the Microsoft Store alias
stub, which prints an ad and exits without creating anything.

Pin the version when it matters: **the DirectML variant requires it**,
because `torch-directml` pins `torch==2.4.1`, which has no wheels for the
newest Python releases. A bare `py` (newest installed) will fail to
resolve it:

```bash
GPU_VARIANT=directml make install PY="py -3.12"
```

### Sidecar placement

Windows is the one platform where Tauri's own resource mechanism works
cleanly: `resource_dir()` there *is* the executable's directory, which is
exactly where the `current_exe()`-relative lookup checks. So
`tauri.windows.conf.json` declares the sidecar via `bundle.resources` and
Tauri places it — no manual patch step, no equivalent of
`macos-release` needed.

`tauri.windows.conf.json` also sets `targets: ["msi"]`, overriding the
base config's macOS-oriented `["dmg", "app"]`.

### Unverified

DirectML's `DirectML.dll` is loaded at runtime via
`torch.ops.load_library`, not as a link-time dependency, and no
PyInstaller hook exists for `torch_directml`. PyInstaller usually
collects binaries sitting inside a package directory anyway, but this
specific case hasn't been confirmed. After a `GPU_VARIANT=directml`
freeze, check that `DirectML.dll` is present next to `torch_directml/` in
the frozen `_internal/` tree. If it's missing, add an explicit
`--add-binary` to `desktop/pyinstaller/proxy-scaler-serve.spec`.

---

## Checklist

1. `git pull` on the build machine.
2. `make install` if dependencies or `GPU_VARIANT` changed.
3. `make sidecar` (or `sidecar-release`) if `proxy_scaler/*` changed.
4. Build: `make release` (Linux) / `make macos-release` (macOS) /
   `cargo tauri build` (Windows).
5. Verify the artifact actually contains the sidecar — on macOS this is
   the mount-and-`ls` above; elsewhere, check the archive listing.
6. Install from the artifact on a clean machine and confirm the server
   starts (Local mode reaching the Decklist tab without the "Couldn't
   start the local server" toast).
7. Upload to Google Drive and update the README link if it changed.
