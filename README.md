# proxy-scaler

Fetch Magic: The Gathering card images from [Scryfall](https://scryfall.com) and upscale them locally for home proxy printing (e.g. upload into [proxxied](https://proxxied.com)).

Unlike [mpc-scryfall](https://github.com/fediazgon/mpc-scryfall), this tool does **not** strip copyright text or add MPC bleed padding — it writes clean upscaled PNGs only.

## Pipeline

1. Import a decklist
2. Resolve each line on Scryfall (exact set/collector, or fuzzy name)
3. Download PNG(s) — double-faced cards produce **front and back** images
4. Upscale to a target print DPI (default **800 DPI** with **SwinIR**)
5. Upload the `output/` folder (or a ZIP of it) into proxxied

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
| 800 (default) | 2000×2800 | Native x4 + Lanczos resize |
| 1200 | 3000×4200 | Native x4 + Lanczos resize |

`--all-dpis` writes all three for each face (reuses the same native upscale when possible).

Weights download automatically into `weights/` on first use (via [Spandrel](https://github.com/chaiNNer-org/spandrel)).

## Install

Python 3.10+. A CUDA GPU (or Apple Silicon MPS) is strongly recommended; CPU works but is slow.

```bash
cd proxy-scaler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Decklist formats

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

Quantities are logged but **one unique image per printing/face** is written (not 20 Plains files). Use the same list in proxxied for counts.

## Usage

### CLI

```bash
proxy-scaler cards.example.txt -o output/ --model swinir --dpi 800
proxy-scaler cards.txt -o output/ --model swinir --all-dpis
python -m proxy_scaler cards.txt --model realesrnet --dpi 1200
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--model` | `swinir` | `swinir`, `realesrnet`, or `realesrgan` |
| `--dpi` | `800` | `600`, `800`, or `1200` |
| `--all-dpis` | off | Write 600 + 800 + 1200 for each face |
| `-o` / `--output` | `output/` | Upscaled PNGs |
| `--cache-dir` | `imgcache/` | Cached native upscales |
| `--weights-dir` | `weights/` | Model weight files |
| `--skip-existing` | off | Don't overwrite existing output files |

Filenames include model + DPI, e.g. `Sol_Ring-C21-263-swinir-800dpi.png`.

### Server

Runs the API and the generation worker together as one managed pair.
This is what the desktop app's **Connect to a server** mode talks to, and
what the Linux package installs as a service.

```bash
proxy-scaler-serve
```

Prints `PROXY_SCALER_READY` on stdout once healthy, then serves until
stopped with Ctrl+C / SIGTERM.

| Flag | Env var | Default | Purpose |
|------|---------|---------|---------|
| `--host` | `PROXY_SCALER_SERVER_HOST` | `127.0.0.1` | Bind address |
| `--port` | `PROXY_SCALER_SERVER_PORT` | `8000` | Bind port |
| `--data-dir` | `PROXY_SCALER_DATA_DIR` | OS per-user dir | Database, worker lock, logs |
| `--no-stdin-shutdown` | — | off | Don't treat stdin EOF as "stop" |
| — | `PROXY_SCALER_DB_PATH` | inside data dir | Database file |
| — | `PROXY_SCALER_WORKER_LOCK_PATH` | inside data dir | Worker lock file |

Flags win over env vars where both are set.

**Accepting connections from other machines** means binding beyond
loopback:

```bash
proxy-scaler-serve --host 0.0.0.0
```

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

#### Server app (Windows/macOS)

A small status window that runs the server for other machines to connect
to, and minimises to the tray so it can be left running. Build it with
`make sidecar && make server-app`, launch with `make server-app-run`.

It shows whether the server is up, the address to paste into the client's
**Connect to a server** box, and a live log. **Allow connections from
other devices** is off by default — the server is loopback-only until you
turn it on, because enabling it exposes an unauthenticated API to the
network (the window says so too).

Note it defaults to port 8000, the same port the desktop client's own
local mode uses. Running both on one machine means the second one fails
to bind; give the server app a different port if you want them
side by side.

#### Debian/Ubuntu package

```bash
sudo apt install ./proxy-scaler_0.1.0_amd64.deb
```

Installs a self-contained bundle to `/opt/proxy-scaler` (no system Python
needed), registers a `proxy-scaler` systemd service running as its own
unprivileged user, and starts it.

| Path | Purpose |
|------|---------|
| `/etc/default/proxy-scaler` | Host, port, data dir — a conffile, so edits survive upgrades |
| `/var/lib/proxy-scaler` | Database, worker lock, generated images |
| `journalctl -u proxy-scaler` | Logs |

The service binds `0.0.0.0` by default, since being reachable is the
point of installing it — re-read the authentication warning above, and
set `PROXY_SCALER_SERVER_HOST=127.0.0.1` then
`systemctl restart proxy-scaler` if you'd rather it stayed local.

`apt remove` keeps `/var/lib/proxy-scaler`; `apt purge` deletes it along
with every project and generated image.

**Building it** (`make sidecar && make deb`, output in `dist/`) has to
happen on Linux, on the architecture you're targeting — PyInstaller
bundles a platform-specific runtime, so a package for a Linux server
cannot be produced from macOS or Windows.

### Desktop app

The primary way to use this day to day — see `desktop/README.md` for the
full Tauri + React client, including the Local/Remote server picker and
how to build it.

## Double-faced cards

If Scryfall provides per-face `image_uris` (transform, MDFC, etc.), both faces are written:

```
Dion_Bahamuts_Dominant-FIN-376-front-swinir-800dpi.png
Bahamut_Warden_of_Light-FIN-376-back-swinir-800dpi.png
```

Split / flip / adventure cards share one front image and produce a single file.

## Apple Silicon

If you hit an MPS operator error:

```bash
PYTORCH_ENABLE_MPS_FALLBACK=1 proxy-scaler cards.txt --model swinir
```
