# proxy-scaler

https://www.proxy-scaler.com/

Fetch Magic: The Gathering card images from [Scryfall](https://scryfall.com), upscale them locally for home proxy printing, and lay them out into a print-ready PDF — cut guides, bleed, and double-faced cards all handled for you. Raw PNGs are also there if you'd rather upload them to a third-party layout tool like [proxxied](https://proxxied.com) instead.

**Join On Discord To Give Feedback!**

<a href="https://discord.gg/bSshvYpKy" rel="nofollow">![Discord](https://img.shields.io/discord/1537518890676912218)</a>

## Quick Start

1. Import a decklist with the format "1 Example Card (set) 123" (on Archidekt and Moxfield, adjust their Export Options to include the set and collector number).
2. Click "Generate upscaled images" and wait till all the tasks are complete.
3. Click the PDF tab, select your Page Size, click "Generate & Download PDF".
**And you're done!**

## Gallery

<table>
  <tr>
    <td valign="top"><img src="https://github.com/user-attachments/assets/27b37586-2be4-4893-81c1-acf362217b2b" alt="Decklist View!" width="100%"></td>
    <td valign="top"><img src="https://github.com/user-attachments/assets/aac89d74-a7c1-4e56-97ee-2e9305052040" alt="Compare Your Generation!" width="100%"></td>
    <td valign="top"><img src="https://github.com/user-attachments/assets/066dae89-1887-4e31-88f9-c57303ada714" alt="Create Your PDF!" width="100%"></td>
  </tr>
</table>

## Card Database & Changing Printings

Click a card's set/collector button in the decklist view to switch it to
any other printing — set, collector number, and (with the right dataset)
language. This is powered by a **local card database** imported from
Scryfall's daily bulk data: the sidebar's "Card database" panel offers
**English only (~80 MB)** or **All languages (~400 MB)** downloads, kept
on the generation server (in Remote mode, on the connected machine — each
server keeps its own copy). Once imported, card resolution runs against
the local database first, so most decklist imports and generations make
no live Scryfall API calls at all; only cards newer than your last import
fall back to the live API. Update it from the same panel whenever the
staleness hint suggests it — it nudges once your copy is more than a week
old, since Secret Lair drops and convention exclusives appear far more
often than that, and a card the corpus has never heard of fails quietly in
the printing picker rather than announcing itself. (Upgrading across a card-database format
change shows "Not imported yet" — just import again.) A first launch with
no database offers the download in a dialog; declining leaves the sidebar
panel as the way in, and the same panel can delete the database again.
The same card can be in a deck once per printing *and language* — an
Italian and an English Sol Ring of the same set import, generate, and
print as two separate cards.

Importing a decklist matches every line before adding it: pick a card
language next to the Import Cards button (strictly that language — lines
without a version in it are listed as errors and left in the box), or
tick **All Languages** to match best-effort across languages. Non-English
lines work too — `1 Aang der Luftnomade 210` finds the German printing by
its printed name — and non-English cards display their printed name in
the deck list, with the English name in the tooltip.

## Future Features

Planned, in roughly the order they're likely to land:

1. **Custom image upload** — use an artist's custom cards, or your own
   images, instead of the Scryfall printing. The [Backs](#backs) tab
   already does the uploading half of this for card backs; what's left is
   pointing a *card* at an uploaded image.
2. **Better project management** — see how much storage space each
   project is consuming, and clean up the ones you no longer need.
3. **MPCFill exploration** — browsing and selecting already-created
   custom art the way other proxy tools do. Custom image upload above is
   the stop-gap until something like this exists.

Have an opinion on the order, or something missing? That's what the
[Discord](https://discord.gg/bSshvYpKy) is for.

## Download

[proxy-scaler.com](https://proxy-scaler.com/#download) — download, install, done.
Prebuilt desktop app, server app, and Linux package.
See [Desktop app](#desktop-app) and [Server](#server) below for what each one is and how to use it.
Building from source (Python CLI, or the desktop app yourself) is covered under [Command-line / library use](#command-line--library-use).

### Which build?

The download page has one build per OS-and-GPU combination — the GPU
variant is baked into the filename (`cuda`, `cuda-legacy`, `directml`,
`rocm`). Pick your row here, then grab the desktop app or server app
flavor of it as needed:

| Your OS | Your GPU | Build to download |
|---|---|---|
| Windows | Nvidia RTX (20-series or newer), or GTX 16-series | `cuda` |
| Windows | Nvidia GTX 10-series or older | `cuda-legacy` (do **not** use on RTX 50 cards — it crashes) |
| Windows | AMD or Intel (any DirectX 12 GPU) | `directml` |
| Windows | None — CPU only | `cuda` (runs fine without a GPU) |
| macOS | Apple Silicon (M-series) | the `macos-arm64` `.dmg` — GPU acceleration (Metal) is built in |
| macOS | Intel Mac | not supported |
| Linux | Nvidia, or CPU only | the standard `linux` build |
| Linux | AMD | the `rocm` build |

### Requirements

The installers are self-contained — Python, PyTorch, and the upscaling
models' loader are all bundled. What you need beyond the download:

- **Windows** — you may need the
  [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)
  (x64) if it isn't already installed. Nothing else.
- **macOS** — Apple Silicon only; nothing to install. GPU acceleration
  needs macOS 12.3+.
- **Linux (desktop app / server app)** — needs WebKitGTK and the tray
  library: `sudo apt install libwebkit2gtk-4.1-0 libgtk-3-0
  libayatana-appindicator3-1`. Requires a distro at least as new as
  Ubuntu 22.04 / Debian 12 (glibc ≥ 2.35).
- **Linux (headless .deb)** — no extra packages; same Ubuntu 22.04 /
  Debian 12 floor.
- **GPU acceleration** is optional — CPU-only works out of the box.
  Nvidia just needs its regular [driver](https://www.nvidia.com/en-us/drivers/)
  (no CUDA toolkit); make sure you grabbed the right build for your GPU
  per the [table above](#which-build).

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

Naming a project is optional and there is nothing to save: paste a
decklist and go, and everything is persisted as you work. Type a name
when you want the project to be findable later — it keeps the images you
have already generated.

**PDF generation** is a full tab of its own: page size presets or custom
dimensions, columns/rows, bleed, spacing, position offset, guide
width/length, and a preferred model/DPI for picking among
already-generated images. It also tells you how many print slots are left
on your last page, and flags any card still missing a generated image —
or, for a double-faced card, missing one of its two faces — before you
print. Guides are switchable per kind and per side: **card guides** (the
marks at each card's own corners) and **page guides** (the lines running
in from the page edge), each independently hideable on front and back
pages.

**Back printing** puts something on the reverse of every card so a sheet
can be duplex-printed. See [Backs](#backs).

### Backs

A **Backs** tab holds your library of card-back images — upload a PNG,
JPEG or WebP, pick one for the project, and optionally mark one as the
default new projects start with. The library is shared across every
project on your machine.

Turn **Print card backs** on from the PDF tab and every sheet is followed
by its reverse, mirrored so the backs land on the right cards once the
paper turns over. Things worth knowing:

- **Flip edge** must match your printer's own duplex setting (*flip on
  long edge* or *flip on short edge* in the print dialog). If the two
  disagree, every card gets someone else's back. Use the **Back of page
  1** preview to check before spending cardstock.
- **Double-faced cards** print as one card with both faces on it, so a
  transform card takes one print slot instead of two. Turn that off and
  each face goes back to being its own card with the back image on its
  reverse.
- **Page order** is interleaved by default (front, back, front, back…),
  which is what duplex printer drivers expect. *All fronts, then all
  backs* is there for hand-feeding a stack through a single-sided printer.
- **Back offsets** nudge only the back pages, for printers whose duplex
  registration drifts a fraction of a millimetre.
- **Upscaling a back** is optional and uses the same models as your cards.
  Upscales live on whichever generation server produced them, so a back
  upscaled on your GPU box prints from its original if you switch to
  Local — it still prints, it just won't be as sharp, and the PDF tab says
  so.

A **Tasks** tab shows the generation queue — what's pending, running,
done, or failed — since upscaling a full decklist runs in the background
and takes real time.

**GPU acceleration**: Nvidia (CUDA) and Apple Silicon (MPS) work out of
the box. AMD works too — ROCm on Linux, DirectML on Windows — see
[GPU support](#gpu-support) below for how a build picks up the right one.

Building it yourself: [`desktop/README.md`](desktop/README.md) for the dev
loop, [`docs/releasing.md`](docs/releasing.md) for producing installers.

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

Skips blank lines, `#` comments, and headers like `Deck` / `Sideboard`.

## Upscale models

| Model | Notes |
|-------|--------|
| `ultrasharp_v2` (default) | General-purpose DAT model, strong on illustration/artwork; best on GPU (CC-BY-NC-SA-4.0) |
| `illustrationjanai` | Trained on digital art/illustrations rather than photos; best on GPU (CC-BY-NC-SA-4.0) |
| `realesrgan_anime_fast` | Compact/lightweight Real-ESRGAN variant tuned for anime; fast enough for CPU-only machines |

All three are trained on illustrated rather than photographic material — and all are x4-native.

## Target DPI

At standard card size (63×88mm):

| DPI | Pixels | How produced |
|-----|--------|----------------|
| 600 | 1488×2079 | Native x4 + Lanczos resize |
| 800 | 1984×2772 | Native x4 + Lanczos resize |
| 1200 (default) | 2976×4157 | Native x4 + Lanczos resize |

`--all-dpis` writes all three for each face (reuses the same native upscale when possible).

## Double-faced cards

If Scryfall provides per-face `image_uris` (transform, MDFC, etc.), both faces are written:

```
Dion_Bahamuts_Dominant-FIN-376-front-ultrasharp_v2-1200dpi.png
Bahamut_Warden_of_Light-FIN-376-back-ultrasharp_v2-1200dpi.png
```

Split / flip / adventure cards share one front image and produce a single file.

The PDF Export will print out both sides always, there's potential I'll make this configurable in the future.

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

<img src="https://github.com/user-attachments/assets/7438adac-746d-48f1-b2d6-81757ca1ee12" alt="Host Your Generation Server!" height="240">

A small status window that runs the server for other machines to connect
to, and minimises to the tray so it can be left running. Download it from
[proxy-scaler.com](https://proxy-scaler.com/#download),
or build it yourself — see [`docs/releasing.md`](docs/releasing.md), which
covers the per-OS steps (macOS in particular needs `make macos-release`
rather than a bare `cargo tauri build`).

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
sudo apt install ./proxy-scaler_0.2.0_amd64.deb
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
cannot be produced from macOS or Windows. `make release` builds this
package alongside the client and server app in one go; see
[`docs/releasing.md`](docs/releasing.md).

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
PYTORCH_ENABLE_MPS_FALLBACK=1 proxy-scaler cards.txt --model ultrasharp_v2
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

### CLI

```bash
proxy-scaler cards.example.txt -o output/ --model ultrasharp_v2 --dpi 1200
proxy-scaler cards.txt -o output/ --model ultrasharp_v2 --all-dpis
python -m proxy_scaler cards.txt --model realesrgan_anime_fast --dpi 800
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--model` | `ultrasharp_v2` | Any model from the [Upscale models](#upscale-models) table below |
| `--dpi` | `1200` | `600`, `800`, or `1200` |
| `--all-dpis` | off | Write 600 + 800 + 1200 for each face |
| `-o` / `--output` | `output/` | Upscaled PNGs |
| `--cache-dir` | `imgcache/` | Cached native upscales |
| `--weights-dir` | `weights/` | Model weight files |
| `--skip-existing` | off | Don't overwrite existing output files |

Filenames include model + DPI, e.g. `Sol_Ring-C21-263-ultrasharp_v2-1200dpi.png`.

Weights download automatically into `weights/` on first use (via
[Spandrel](https://github.com/chaiNNer-org/spandrel)).
