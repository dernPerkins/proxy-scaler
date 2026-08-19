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
per-file release-asset limit, so releases are distributed from the R2
bucket behind dl.proxy-scaler.com rather than attached to a GitHub
release. Splitting into <2GB chunks would also work but hasn't been
needed.

## Versioning & the update manifest

**Bump the version first, in one command.** The version lives in seven
files (pyproject.toml, `proxy_scaler/__init__.py`, both
`tauri.conf.json`s, both `Cargo.toml`s, the frontend `package.json`) and
nothing structural keeps them in agreement — installed apps report the
Cargo/tauri copies, the API's `/api/version` reports the Python copy,
and artifact filenames use pyproject's. So:

```bash
make set-version VERSION=0.2.0    # step zero of every release
make check-version                # verify; also runs automatically at the
                                  # start of 'release' and 'macos-release'
```

`check-version` failing the release targets is deliberate: a drifted
release is how you ship installers that claim one version while the app
reports another — and it breaks the update check, which compares these
numbers.

**The update manifest (`dist/latest.json`)** is what installed apps poll
on boot (`GET https://dl.proxy-scaler.com/latest.json` — see
`desktop/src-tauri/src/update.rs`) to offer the user an update. It
carries the latest version, release notes, and per-artifact
`size`/`sha256` (computed for you — the client refuses to launch an
installer that doesn't match them). `packaging/generate-manifest.py`
builds it by scanning `dist/`:

- Runs automatically at the end of `make release` and `make macos-release`.
- Run `make manifest` by hand after the Windows passes.
- It **merges**: each run adds/replaces entries for the artifacts present
  in its own `dist/` and keeps the rest, because the three platforms
  build on different machines. Seed `dist/latest.json` from the previous
  machine (or from R2) to accumulate the full set.
- `NOTES="Fixed X, added Y" make manifest` sets the release-notes text
  shown inside the app's update prompt.

Upload `latest.json` to R2 **last**, after every artifact it references
is uploaded — from the moment it lands, installed apps will offer
downloads from the URLs inside it.

## GPU variants

`torch` builds for different GPU vendors are mutually exclusive — ROCm
and DirectML *replace* the default CUDA-capable wheel rather than adding
to it. So a GPU variant is chosen at install time, and produces a
separate artifact:

```bash
make install                          # default: CUDA-capable on Linux (also fine CPU-only)
GPU_VARIANT=rocm make install         # AMD on Linux
GPU_VARIANT=directml make install     # AMD on Windows
```

On **Windows** the default install is CPU-only — PyPI's Windows torch
wheels ship without CUDA — so an Nvidia-on-Windows build needs an extra
CUDA-wheel step after `make install`; see [Windows](#windows).

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

The window styling (background image with the drag arrow, fixed window
size and icon positions) is applied by `packaging/build-dmg-macos.sh`:
it mounts a read-write dmg and drives Finder over AppleScript to write
the volume's `.DS_Store`, then compresses to the final UDZO. That means
the dmg build needs a logged-in GUI session, and the first run prompts
for permission to control Finder (Privacy & Security → Automation). The
background pngs live in `desktop/*/dmg/` — authored at 2x with 144-dpi
metadata so Finder renders them at 1x; image editors can strip that dpi
chunk on export (GIMP: keep "Save resolution" ticked), which makes the
background render double-size.

### Code signing

Two modes, selected by whether `MACOS_SIGN_IDENTITY` is set:

**Default: ad-hoc.** `macos-bundle-{client,server-app}-sidecar` runs
`codesign --force --sign -` on the `.app` immediately after the
sidecar is copied in, and verifies it. This is not a substitute for
notarization — no Developer ID means Gatekeeper still blocks a
downloaded build ("could not verify … free of malware"; since macOS 15
the user has to approve it under Privacy & Security → Open Anyway).

**Release: Developer ID + notarization.** See "Developer ID &
notarization" below.

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

The inside-out pass that replaces outer-only signing for releases lives
in `packaging/sign-macos.sh` — sidecar Mach-O files individually, then
the outer `.app` last, still without `--deep`.

### Developer ID & notarization

Distribution stays exactly as it is — a `.dmg` uploaded wherever we
like. Notarization is Apple's automated malware scan for apps
distributed *outside* the App Store; nothing here involves App Store
review or hosting.

One-time setup on the build Mac (needs the paid Apple Developer
account):

1. Create a **Developer ID Application** certificate at
   developer.apple.com → Certificates, and install it in the login
   keychain (or via Xcode → Settings → Accounts → Manage Certificates).
   Confirm with `security find-identity -v -p codesigning` — the line
   you want reads `Developer ID Application: <name> (<TEAMID>)`.
2. Create an app-specific password at appleid.apple.com, then store
   notary credentials in the keychain:

   ```bash
   xcrun notarytool store-credentials proxy-scaler-notary \
       --apple-id <appleid email> --team-id <TEAMID> \
       --password <app-specific password>
   ```

Then a release build is:

```bash
MACOS_SIGN_IDENTITY="Developer ID Application: <name> (<TEAMID>)" \
MACOS_NOTARY_PROFILE=proxy-scaler-notary \
make macos-release
```

With the identity set, `codesign_app` switches from the ad-hoc re-seal
to `packaging/sign-macos.sh` (hardened runtime, secure timestamps,
sidecar entitlements from `packaging/macos-entitlements.plist`), and
after each dmg is built `packaging/notarize-macos.sh` signs it, submits
it with `notarytool --wait` (typically a few minutes per dmg), and
staples the ticket so Gatekeeper can verify offline. Identity without
profile signs but skips notarization — useful for testing the signing
pass, not for anything users download.

If notarization is rejected, the script prints the `notarytool log`
command that fetches Apple's per-file report; the usual culprits are a
sidecar Mach-O that missed signing or a missing entitlement (the
sidecar's CPython needs `allow-unsigned-executable-memory` and
`disable-library-validation` under the hardened runtime; both are in the
plist).

Verify a finished dmg the way a user's Mac will:

```bash
spctl --assess --type open --context context:primary-signature -v dist/<name>.dmg
xcrun stapler validate dist/<name>.dmg
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

Every command below runs from **Git Bash or MSYS2** (not
`cmd`/PowerShell — the Makefile detects those environments to find the
venv's `Scripts/` directory and `.exe` suffixes), from the repo root.

There are three Windows variants, and the venv holds exactly one at a
time, so each is a full pass starting from a deleted venv. All passes
write their MSIs to the same `target/release/bundle/msi/` paths, so the
assemble step at the end of each pass is what keeps them from
overwriting each other.

| Variant | torch wheel | GPUs covered |
|---|---|---|
| `cuda` | cu128 (torch 2.11) | all RTX cards (sm_75 Turing → sm_120 Blackwell) |
| `cuda-legacy` | cu126 (torch 2.13) | GTX 10-series → RTX 40 (sm_50 → sm_90); **crashes on RTX 50** |
| `directml` | torch-directml | AMD/Intel (any DX12 GPU) |

Two CUDA tiers exist because torch wheels bake in GPU kernels per
compute capability, and no single wheel covers both a GTX 10-series
(sm_61) and an RTX 50 (sm_120) — when PyTorch added Blackwell kernels
in cu128 it dropped everything pre-Turing. Shipping the wrong tier
fails at generation time with "CUDA error: no kernel image is available
for execution on the device". After any torch swap, the arch-list
checkpoint below is what catches this before a multi-GB build.

**The MSI is not a single file.** The distributable set is a small
`.msi` (landing in `target/release/bundle/msi/`) plus `cab1.cab`…
`cabN.cab` holding the actual payload — which `light.exe` leaves behind
in `target/release/wix/x64/`, because Tauri only copies the `.msi` out.
The assemble step at the end of each pass gathers them into one `dist/`
folder per app. Every file ships together: the `.msi` finds its cabs by
name in its own directory, so a user who downloads them must put them
all in one folder before running the installer (and the two apps' cab
names collide, so client and server sets can never share a folder).

The split comes from `desktop/wix/main.wxs` (wired in via each app's
`tauri.windows.conf.json`), a copy of Tauri's default WiX template with
one change: external split cabinets instead of one embedded cabinet. The
embedded default hard-fails on the ~4GB CUDA sidecar — `light.exe` dies
with `LGHT0001 Catastrophic failure` in `WixCreateCab`, because both the
CAB format and the `.msi` container cap out near 2GB.

First time only: `cd desktop/frontend && npm install && cd ../..`

### Pass 1 — Nvidia RTX (`cuda`, cu128)

```bash
rm -rf .venv
make install PY="py -3.12"

# PyPI's *Windows* torch wheels are CPU-only (unlike Linux), so layer the
# CUDA wheel on top. torch is pinned because a bare "torch" on these
# indexes can resolve to an older version (torchvision's joint
# constraints); bump the pin deliberately, checking what the index
# actually has first: pip index versions torch --index-url <index>
.venv/Scripts/pip install --force-reinstall "torch==2.11.0" torchvision --index-url https://download.pytorch.org/whl/cu128

# CHECKPOINT: must print a "+cu..." version, True, and an arch list
# containing BOTH sm_75 (RTX 20) and sm_120 (RTX 50). If not, stop —
# a freeze from this venv ships a server that's CPU-only or crashes
# on whole GPU generations.
.venv/Scripts/python -c "import torch; print(torch.__version__, torch.cuda.is_available()); print(torch.cuda.get_arch_list())"

make sidecar
cd desktop/src-tauri && cargo tauri build && cd ../..      # client MSI
cd desktop/server-app && cargo tauri build && cd ../..     # server app MSI

# Assemble each app's distributable set (.msi + its cab files) into its
# own dist/ folder. The cabs are in wix/x64, NOT next to the built .msi.
mkdir -p dist/proxy-scaler-client_0.1.0_windows-x64_cuda dist/proxy-scaler-server-app_0.1.0_windows-x64_cuda
cp "desktop/src-tauri/target/release/bundle/msi/Proxy Scaler_0.1.0_x64_en-US.msi" desktop/src-tauri/target/release/wix/x64/cab*.cab dist/proxy-scaler-client_0.1.0_windows-x64_cuda/
cp "desktop/server-app/target/release/bundle/msi/Proxy Scaler Server_0.1.0_x64_en-US.msi" desktop/server-app/target/release/wix/x64/cab*.cab dist/proxy-scaler-server-app_0.1.0_windows-x64_cuda/

# Zip each folder — the zip is what gets uploaded: one file per app, and
# nobody can download the .msi without the cabs it needs. (tar here is
# Windows' built-in bsdtar; -a picks zip format from the output name.)
tar -a -c -f dist/proxy-scaler-client_0.1.0_windows-x64_cuda.zip -C dist proxy-scaler-client_0.1.0_windows-x64_cuda
tar -a -c -f dist/proxy-scaler-server-app_0.1.0_windows-x64_cuda.zip -C dist proxy-scaler-server-app_0.1.0_windows-x64_cuda
```

### Pass 2 — Nvidia GTX legacy (`cuda-legacy`, cu126)

Identical to Pass 1 except for the wheel index, the checkpoint's
expected arch list, and `_cuda-legacy` in every artifact name:

```bash
rm -rf .venv
make install PY="py -3.12"
.venv/Scripts/pip install --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu126

# CHECKPOINT: arch list must contain sm_61 (GTX 10-series) — that's the
# card this whole variant exists for.
.venv/Scripts/python -c "import torch; print(torch.__version__, torch.cuda.is_available()); print(torch.cuda.get_arch_list())"

make sidecar
cd desktop/src-tauri && cargo tauri build && cd ../..
cd desktop/server-app && cargo tauri build && cd ../..
mkdir -p dist/proxy-scaler-client_0.1.0_windows-x64_cuda-legacy dist/proxy-scaler-server-app_0.1.0_windows-x64_cuda-legacy
cp "desktop/src-tauri/target/release/bundle/msi/Proxy Scaler_0.1.0_x64_en-US.msi" desktop/src-tauri/target/release/wix/x64/cab*.cab dist/proxy-scaler-client_0.1.0_windows-x64_cuda-legacy/
cp "desktop/server-app/target/release/bundle/msi/Proxy Scaler Server_0.1.0_x64_en-US.msi" desktop/server-app/target/release/wix/x64/cab*.cab dist/proxy-scaler-server-app_0.1.0_windows-x64_cuda-legacy/
tar -a -c -f dist/proxy-scaler-client_0.1.0_windows-x64_cuda-legacy.zip -C dist proxy-scaler-client_0.1.0_windows-x64_cuda-legacy
tar -a -c -f dist/proxy-scaler-server-app_0.1.0_windows-x64_cuda-legacy.zip -C dist proxy-scaler-server-app_0.1.0_windows-x64_cuda-legacy
```

### Pass 3 — AMD (DirectML)

The `PY="py -3.12"` pin is load-bearing here: `torch-directml` pins
`torch==2.4.1`, which has no wheels for the newest Python releases, and
a bare `py` (newest installed) will fail to resolve it. (`make install`
uses the `py` launcher rather than `python3` in all cases — on Windows
`python3` is usually the Microsoft Store alias stub, which prints an ad
and exits without creating anything.)

```bash
rm -rf .venv
GPU_VARIANT=directml make install PY="py -3.12"
make sidecar

# CHECKPOINT: this file must exist. PyInstaller has no torch_directml
# hook, and a freeze without it ships a server that crashes at startup
# with "Failed to load dynlib/dll ... torch_directml\DirectML.dll" —
# this happened. If it's missing, fix the collection in
# desktop/pyinstaller/proxy-scaler-serve.spec before building installers.
ls desktop/pyinstaller/dist/proxy-scaler-serve/_internal/torch_directml/DirectML.dll

cd desktop/src-tauri && cargo tauri build && cd ../..
cd desktop/server-app && cargo tauri build && cd ../..
mkdir -p dist/proxy-scaler-client_0.1.0_windows-x64_directml dist/proxy-scaler-server-app_0.1.0_windows-x64_directml
cp "desktop/src-tauri/target/release/bundle/msi/Proxy Scaler_0.1.0_x64_en-US.msi" desktop/src-tauri/target/release/wix/x64/cab*.cab dist/proxy-scaler-client_0.1.0_windows-x64_directml/
cp "desktop/server-app/target/release/bundle/msi/Proxy Scaler Server_0.1.0_x64_en-US.msi" desktop/server-app/target/release/wix/x64/cab*.cab dist/proxy-scaler-server-app_0.1.0_windows-x64_directml/
tar -a -c -f dist/proxy-scaler-client_0.1.0_windows-x64_directml.zip -C dist proxy-scaler-client_0.1.0_windows-x64_directml
tar -a -c -f dist/proxy-scaler-server-app_0.1.0_windows-x64_directml.zip -C dist proxy-scaler-server-app_0.1.0_windows-x64_directml
```

### Single-file setup.exe (the uploaded artifact)

The zips work, but the preferred distributable is one `setup.exe` per
app per variant: nothing to extract, and nobody can end up with the
`.msi` separated from its cabs. It's produced by Inno Setup wrapping the
assembled dist folder — the wrapper extracts the MSI set to `{tmp}` and
hands off to Windows Installer, so the MSI remains the source of truth
for install/upgrade/uninstall (see `desktop/inno/setup.iss`).

Why Inno and not WiX's own bootstrapper: Burn's attached container is a
CAB, so the same 2GB ceiling that forced external cabs kills the bundle
too — verified failing on both WiX 3.14 (`LGHT0306`) and WiX v5
(`0x80004005` compressing `bundle-attached.cab`). Inno 6 has no such
limit (a 2.47GB single exe built and verified).

Install once: `winget install JRSoftware.InnoSetup` — lands in
`%LOCALAPPDATA%\Programs\Inno Setup 6`.

```bash
ISCC="$LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe"

"$ISCC" "/DAppName=Proxy Scaler" /DAppVersion=0.1.0 \
  "/DMsiDir=$(pwd)/dist/proxy-scaler-client_0.1.0_windows-x64_cuda" \
  "/DMsiName=Proxy Scaler_0.1.0_x64_en-US.msi" \
  "/DIconPath=$(pwd)/desktop/src-tauri/icons/icon.ico" \
  "/DOutputDir=$(pwd)/dist" \
  /DOutputBaseName=proxy-scaler-client_0.1.0_windows-x64_cuda-setup \
  /Q desktop/inno/setup.iss

"$ISCC" "/DAppName=Proxy Scaler Server" /DAppVersion=0.1.0 \
  "/DMsiDir=$(pwd)/dist/proxy-scaler-server-app_0.1.0_windows-x64_cuda" \
  "/DMsiName=Proxy Scaler Server_0.1.0_x64_en-US.msi" \
  "/DIconPath=$(pwd)/desktop/server-app/icons/icon.ico" \
  "/DOutputDir=$(pwd)/dist" \
  /DOutputBaseName=proxy-scaler-server-app_0.1.0_windows-x64_cuda-setup \
  /Q desktop/inno/setup.iss
```

Repeat per variant by swapping `_cuda` for `_cuda-legacy` / `_directml`
in `MsiDir` and `OutputBaseName`.

### After the passes

Uninstall any previous Proxy Scaler install, then on a clean machine
download a zip and **extract it fully** before running the `.msi` — the
`.msi` alone installs nothing; its payload is the cab files, which must
sit in the same directory, and running it from inside a zip preview
window can't see them. Then confirm Local mode reaches the Decklist tab
without the "Couldn't start the local server" toast. On the CUDA builds,
also confirm the app reports a GPU device rather than CPU, and run an
actual generation — the "no kernel image" class of failure only shows up
when a kernel launches, not at startup.

### Why there's no patch step (unlike macOS)

Windows is the one platform where Tauri's own resource mechanism works
cleanly: `resource_dir()` there *is* the executable's directory, exactly
where the `current_exe()`-relative lookup checks. So
`tauri.windows.conf.json` declares the sidecar via `bundle.resources`
(and sets `targets: ["msi"]`, overriding the base config's
macOS-oriented `["dmg", "app"]`), and Tauri places it — no equivalent of
`macos-release` needed.

---

## Checklist

1. `git pull` on the build machine.
2. `make set-version VERSION=x.y.z` (once, on one machine, committed —
   the other build machines pick it up via `git pull`). `make release` /
   `make macos-release` fail on version drift; run `make check-version`
   to see it directly.
3. `make install` if dependencies or `GPU_VARIANT` changed.
4. `make sidecar` (or `sidecar-release`) if `proxy_scaler/*` changed —
   and always after a version bump, so `/api/version` reports the new one.
5. Build: `make release` (Linux) / `make macos-release` (macOS) /
   the per-variant passes in [Windows](#windows).
6. Verify the artifact actually contains the sidecar — on macOS this is
   the mount-and-`ls` above; elsewhere, check the archive listing.
7. Install from the artifact on a clean machine and confirm the server
   starts (Local mode reaching the Decklist tab without the "Couldn't
   start the local server" toast).
8. `make manifest` after the Windows passes (Linux/macOS release targets
   already ran it) — seeding `dist/latest.json` from the previous build
   machine so entries accumulate; see
   [Versioning & the update manifest](#versioning--the-update-manifest).
9. Upload to the R2 bucket behind dl.proxy-scaler.com (`rclone copy
   dist/ R2:proxy-scaler-site --include "proxy-scaler*"` once an `R2:`
   remote is configured) and update the download buttons on the
   [site](https://github.com/dernPerkins/proxy-scaler-site) if artifact
   names changed.
10. Upload `dist/latest.json` to the same bucket **last** — installed
    apps start offering the update the moment it lands, so every URL it
    references must already resolve.
