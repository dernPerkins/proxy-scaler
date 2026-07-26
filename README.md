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

### Streamlit UI

```bash
streamlit run app.py
```

**Tabs** (synced to the URL query string):

| Tab | URL | Role |
|-----|-----|------|
| Decklist (default) | `?tab=decklist` | Paste list, generate, compare, regenerate |
| PDF Generation | `?tab=pdf` | Placeholder for future PDF export |

**Projects** — decklist text, settings, and gallery metadata persist in SQLite at `data/proxy_scaler.db` (created on first run; `data/*.db` is gitignored). Use the Project bar to **Save** / **Save As** / **Load** / **New** / **Delete**. Image files stay on disk under `output/` / `imgcache/`; deleting a project removes DB rows only.

Sidebar (Decklist tab):

- Model picker (SwinIR default)
- Target DPI (800 default) or **Generate all DPIs**
- **Show all DPIs in one row** — original + each DPI side-by-side
- **Delete all generated images & cache** — clears `output/` and `imgcache/` (keeps weights); requires confirm checkbox

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
