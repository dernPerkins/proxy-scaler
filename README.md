# proxy-scaler

Fetch Magic: The Gathering card images from [Scryfall](https://scryfall.com), upscale them locally for home proxy printing, and lay them out into a print-ready PDF — cut guides, bleed, and double-faced cards all handled for you. Raw PNGs are also there if you'd rather upload them to a third-party layout tool like [proxxied](https://proxxied.com) instead.

Unlike [mpc-scryfall](https://github.com/fediazgon/mpc-scryfall), this tool does **not** strip copyright text or add MPC bleed padding — it writes clean upscaled PNGs, and its own PDF export adds bleed/guides on top of those, rather than baking anything into the source image.

## Download

Prebuilt desktop app, server app, and Linux package are on
[Google Drive](https://drive.google.com/drive/folders/1uqiXgRyMGbvMknasZXFH2d9jXbCa1ian?usp=drive_link) — download, install, done. See
[Desktop app](#desktop-app) and [Server](#server) below for what each one
is and how to use it. Building from source (Python CLI, or the desktop app
yourself) is covered under [Command-line / library use](#command-line--library-use).

## Pipeline

1. Import a decklist
2. Resolve each line on Scryfall (exact set/collector, or fuzzy name)
3. Download PNG(s) — double-faced cards produce **front and back** images
4. Upscale to a target print DPI (default **1200 DPI** with **SwinIR**)
5. Either export a print-ready PDF directly (bleed, cut guides, page
   layout, quantities) or hand the raw PNGs to something else, e.g.
   uploading `output/` into proxxied

## Desktop app

The primary way to use this day to day — a Tauri + React app with two
generation modes, picked fresh on every launch:

- **Use this device** (Local) — spawns a bundled generation server on your
  own machine, no setup beyond installing the app.
- **Connect to a server** (Remote) — points at a [server app](#server-app-windowsmacos)
  or [headless Linux service](#debianubuntu-package) running elsewhere,
  e.g. a GPU box reached over [Tailscale](https://tailscale.com).

Projects (decklist text, parsed cards, generation settings, PDF layout
settings) live in a local SQLite file on the machine running the client,
independent of which generation server is currently connected — switching
Local/Remote mid-session never touches your project data.

**PDF generation** is a full tab of its own: page size presets or custom
dimensions, columns/rows, bleed, spacing, position offset, cut-guide
width/length, and a preferred model/DPI for picking among
already-generated images. It also tells you how many print slots are left
on your last page (a double-faced card counts as two), and flags any card
still missing a generated image — or, for a double-faced card, missing
one of its two faces — before you print.

**GPU acceleration**: Nvidia (CUDA) and Apple Silicon (MPS) work out of
the box. AMD works too — ROCm on Linux, DirectML on Windows — see
[GPU support](#gpu-support) below for how a build picks up the right one.

See [`desktop/README.md`](desktop/README.md) for building the client
yourself.

## Server

Runs the API and the generation worker together as one managed pair. This
is what the desktop app's **Connect to a server** mode talks to.

```bash
proxy-scaler-serve
```

Prints `PROXY_SCALER_READY` on stdout once healthy, then serves until
stopped with Ctrl+C / SIGTERM.

| Flag | Env var | Default | Purpose |
|------|---------|---------|---------|
| `--host` | `PROXY_SCALER_SERVER_HOST` | `127.0.0.1` | Bind address |
| `--bind-all` | — | off | Shorthand for `--host 0.0.0.0` — overrides `--host`/the env var if either is also set |
| `--port` | `PROXY_SCALER_SERVER_PORT` | `13207` | Bind port |
| `--data-dir` | `PROXY_SCALER_DATA_DIR` | OS per-user dir | Database, worker lock, logs |
| `--no-stdin-shutdown` | — | off | Don't treat stdin EOF as "stop" |
| — | `PROXY_SCALER_DB_PATH` | inside data dir | Database file |
| — | `PROXY_SCALER_WORKER_LOCK_PATH` | inside data dir | Worker lock file |

Flags win over env vars where both are set.

The default port, `13207`, is just **M-T-G** spelled out by letter
position (13th, 20th, 7th) — picked to dodge the usual
`8000`/`8080`/`8888`/`9000`/etc collisions with other dev tools or
self-hosted services that might already be running on the same box or
Tailscale tailnet.

**Accepting connections from other machines** means binding beyond
loopback:

```bash
proxy-scaler-serve --host 0.0.0.0
# or, equivalently:
proxy-scaler-serve --bind-all
```

`--bind-all` exists because typing your own hostname instead is a real
trap: on Debian in particular, a machine's own hostname commonly resolves
via `/etc/hosts` to `127.0.1.1`, which is still loopback-only — binding to
it silently stays local-only, with no error to signal that connecting from
another machine (e.g. over Tailscale) was never going to work.

> **There is no authentication.** Anyone who can reach the port can read
> and write projects, and queue generation work on your GPU. Only bind
> beyond `127.0.0.1` on a network you trust — a home LAN, or better, a
> private overlay network like [Tailscale](https://tailscale.com), which
> is what the desktop app's connect screen recommends.

Stdin EOF is treated as a shutdown request, because that's how the
desktop app stops its embedded server cleanly. Service managers hand a
process `/dev/null`, which reads as immediate EOF — that case is
detected and ignored automatically, so systemd and Docker work without
`--no-stdin-shutdown`; the flag is there for anything that slips past
the check.

### Server app (Windows/macOS)

A small status window that runs the server for other machines to connect
to, and minimises to the tray so it can be left running. Download it from
[Google Drive](https://drive.google.com/drive/folders/1uqiXgRyMGbvMknasZXFH2d9jXbCa1ian?usp=drive_link), or build it yourself with
`make sidecar && make server-app` (`make macos-bundle-server-app-sidecar`
too, on macOS — see [`desktop/README.md`](desktop/README.md)).

It shows whether the server is up, the address to paste into the client's
**Connect to a server** box, and a live log. **Allow connections from
other devices** is off by default — the server is loopback-only until you
turn it on, because enabling it exposes an unauthenticated API to the
network (the window says so too).

Note it defaults to port 13207, the same port the desktop client's own
local mode uses. Running both on one machine means the second one fails
to bind; give the server app a different port if you want them
side by side.

### Debian/Ubuntu package

```bash
sudo apt install ./proxy-scaler_0.1.0_amd64.deb
```

Installs a self-contained bundle to `/opt/proxy-scaler` (no system Python
needed) and puts `proxy-scaler-serve` on `PATH`. It does **not** start
anything automatically — this is a CLI tool by default, not an always-on
service:

```bash
proxy-scaler-serve --port 13207
```

If you'd rather it ran persistently in the background instead, a
`proxy-scaler` systemd service (running as its own unprivileged user) is
installed but left disabled — opt in explicitly:

```bash
sudo systemctl enable --now proxy-scaler
```

| Path | Purpose |
|------|---------|
| `/etc/default/proxy-scaler` | Host, port, data dir for the systemd service — a conffile, so edits survive upgrades. Not consulted when running `proxy-scaler-serve` directly; use its own flags/env vars for that (see the Server section above) |
| `/var/lib/proxy-scaler` | Database, worker lock, generated images (service mode only) |
| `journalctl -u proxy-scaler` | Logs (service mode only) |

The service, if you enable it, binds `0.0.0.0` by default — re-read the
authentication warning above, and set `PROXY_SCALER_SERVER_HOST=127.0.0.1`
in `/etc/default/proxy-scaler` then `systemctl restart proxy-scaler` if
you'd rather it stayed local. Upgrading the package (`apt install` over
an existing install) restarts the service automatically *only if you'd
already enabled it* — a fresh install never auto-starts anything.

`apt remove` keeps `/var/lib/proxy-scaler`; `apt purge` deletes it along
with every project and generated image.

**Building it** (`make sidecar && make deb`, output in `dist/`) has to
happen on Linux, on the architecture you're targeting — PyInstaller
bundles a platform-specific runtime, so a package for a Linux server
cannot be produced from macOS or Windows. See
[`desktop/README.md`](desktop/README.md) for the full release build
(`make release`), which produces the client, server app, and this package
together.

## GPU support

| Vendor | Platform | How | Code changes needed |
|---|---|---|---|
| Nvidia | any | CUDA — the default `pip`/build install already covers this | none |
| Apple | macOS | Metal (MPS) — built in, works out of the box | none |
| AMD | Linux | ROCm — a different torch wheel, not a different code path (ROCm reports through the same CUDA APIs) | build-time only |
| AMD | Windows | DirectML — the only realistic path, since AMD doesn't ship ROCm for Windows | handled in `resolve_device()`/`device_kind()` already |

Building for AMD needs a variant-specific install, since ROCm/DirectML
replace `torch` with an incompatible build rather than adding to it:

```bash
GPU_VARIANT=rocm make install sidecar       # AMD on Linux
GPU_VARIANT=directml make install sidecar   # AMD on Windows
```

Plain `make install`/`pip install -e .` (no `GPU_VARIANT`) gets you the
default CUDA-capable build, which also runs fine CPU-only on a machine
with no GPU at all — just slower.

If you hit an MPS operator error on Apple Silicon:

```bash
PYTORCH_ENABLE_MPS_FALLBACK=1 proxy-scaler cards.txt --model swinir
```

## Command-line / library use

For scripting, headless batch runs, or hacking on the pipeline directly —
most people want the [desktop app](#desktop-app) instead.

### Install

Python 3.10+.

```bash
git clone https://github.com/dernPerkins/proxy-scaler.git
cd proxy-scaler
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

(`GPU_VARIANT=rocm make install` / `GPU_VARIANT=directml make install` if
you're on AMD — see [GPU support](#gpu-support).)

### Decklist formats

Both formats can be mixed in one file.

**Exact printing (preferred when art matters):**

```
1 Abandoned Air Temple (tla) 263
1 Dion, Bahamut's Dominant // Bahamut, Warden of Light (fin) 376
1 Knight Exemplar (plst) DDG-14
20 Plains (mh2) 482
```

**Name only (Arena-style; Scryfall chooses a default printing):**

```
1 Sol Ring
4 Lightning Bolt
```

Skip blank lines, `#` comments, and headers like `Deck` / `Sideboard`.

Quantities are logged but **one unique image per printing/face** is
written (not 20 Plains files) — the desktop app's PDF export is what
actually expands a printing back out to its full quantity on the page;
use the same decklist quantities if uploading raw PNGs to a third-party
layout tool like proxxied instead.

### CLI

```bash
proxy-scaler cards.example.txt -o output/ --model swinir --dpi 1200
proxy-scaler cards.txt -o output/ --model swinir --all-dpis
python -m proxy_scaler cards.txt --model ultrasharp_v2 --dpi 800
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--model` | `swinir` | Any model from the [Upscale models](#upscale-models) table below |
| `--dpi` | `1200` | `600`, `800`, or `1200` |
| `--all-dpis` | off | Write 600 + 800 + 1200 for each face |
| `-o` / `--output` | `output/` | Upscaled PNGs |
| `--cache-dir` | `imgcache/` | Cached native upscales |
| `--weights-dir` | `weights/` | Model weight files |
| `--skip-existing` | off | Don't overwrite existing output files |

Filenames include model + DPI, e.g. `Sol_Ring-C21-263-swinir-1200dpi.png`.

Weights download automatically into `weights/` on first use (via
[Spandrel](https://github.com/chaiNNer-org/spandrel)).

## Upscale models

| Model | Notes |
|-------|--------|
| `swinir` (default) | Classical SwinIR (DF2K); fidelity-first, slower |
| `realesrnet` | Less hallucination; often cleaner text/symbols (x4-native) |
| `realesrgan` | Fast; invents more detail |
| `realesrgan_anime` | Official Real-ESRGAN variant tuned for illustrated/non-photo art (x4-native) |
| `illustrationjanai` | Trained on digital art/illustrations rather than photos (x4-native, CC-BY-NC-SA-4.0) |
| `ultrasharp_v2` | General-purpose DAT model, strong on illustration/artwork (x4-native, CC-BY-NC-SA-4.0) |
| `hat` | Newer transformer architecture (Hybrid Attention Transformer), high fidelity (x4-native) |

The first three are trained mainly on photographic benchmark datasets. Since
MTG card art is illustrated, not photographic, the last four are worth trying
if the photo-trained models introduce artifacts on card art specifically.

## Target DPI

At standard card size (2.5″×3.5″):

| DPI | Pixels | How produced |
|-----|--------|----------------|
| 600 | 1500×2100 | Native x2 when available, else x4 + Lanczos |
| 800 | 2000×2800 | Native x4 + Lanczos resize |
| 1200 (default) | 3000×4200 | Native x4 + Lanczos resize |

`--all-dpis` writes all three for each face (reuses the same native upscale when possible).

## Double-faced cards

If Scryfall provides per-face `image_uris` (transform, MDFC, etc.), both faces are written:

```
Dion_Bahamuts_Dominant-FIN-376-front-swinir-1200dpi.png
Bahamut_Warden_of_Light-FIN-376-back-swinir-1200dpi.png
```

Split / flip / adventure cards share one front image and produce a single file.

The desktop app's PDF export goes further: it tracks how many faces each
card actually has (from Scryfall), so a double-faced card missing its back
face gets flagged instead of silently printing half a card.
