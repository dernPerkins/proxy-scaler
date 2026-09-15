# Windows validation: five-rung auto tile ladder + allocator change

> **Status (2026-09-14):** run once on the RTX 5080 laptop against `0d9e5a4` — results in `windows-auto-tile-validation-results.md`. Confirmed there: the cu128 wheel ignores `expandable_segments` (reserved ≈ 2× allocated; the calibrated headroom of ~2.3 covers it), the OOM retry works only with the driver's sysmem fallback disabled, and a headroom ratchet found in that run is fixed in `0d9e5a4`. This revision folds the agent's corrections in. **Still open: the AMD 7900 XTX (§6).**

You are validating a change to proxy-scaler on Windows. Two machines are in scope:

| machine | GPU | build variant | what the ladder should do |
|---|---|---|---|
| Laptop | RTX 5080 16 GB | `cuda` (cu128 torch) | full ladder active: untiled / 640 / 512 / 384 / 256 picked from free VRAM |
| Desktop | AMD 7900 XTX 24 GB | `directml` | ladder **inactive** by design (CUDA-only probe); confirm nothing regressed |

Do not change application code. If something fails, capture the output and report it using the template at the end. Everything below is read-and-run.

## 1. What changed (so you know what "correct" looks like)

Files: `proxy_scaler/upscale.py`, `proxy_scaler/worker.py`, `tests/test_oom.py`, `tests/test_worker.py`.

- **Auto tile ladder** for the heavy models (UltraSharpV2, IllustrationJaNai) is now five rungs, largest first: `0` (untiled), `640`, `512`, `384`, `256`. Before each card the code probes free VRAM and picks the largest rung whose *estimated* need fits. The untiled rung also requires at least **9 GiB free**. A Scryfall card is 745 px wide, so any tile ≥ 745 is the same as untiled.
- **Headroom self-calibrates.** The first card of a process gates at 2× the estimate; afterwards the observed `reserved / allocated` ratio × 1.15 (never below 1.4×) replaces it.
- **OOM retry re-probes.** If the chosen rung OOMs anyway, the code re-reads free VRAM and jumps to the rung that fits (one step down if the probe is unavailable), then CPU only after the 256 floor fails. The retry now runs outside the `except` block that caught the OOM; previously the live traceback pinned the failed pass's tensors so nothing was ever freed.
- **Light models** (Anime Fast, UltraSharpV2 Lite) stay untiled; on OOM they get one retry at 384 before CPU.
- **Manual tile** (non-zero in the Decklist sidebar) is never touched by any of this.
- **Worker sets `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** before torch loads (`worker.py::_configure_cuda_allocator`, `setdefault`, so a pre-set value wins). On Linux this cut reserved VRAM from ~2× allocated to ~1.04×. **Whether the Windows cu128 wheel honours it is the main open question of this validation.**

Gate values for a 745×1040 card in bf16 (what the estimator computes; you don't need to reproduce them, just recognise the picks):

| rung | need at 1.4× (steady state) | need at 2.0× (first card) |
|---|---|---|
| untiled | 9.0 GiB (explicit floor) | 12.6 GiB |
| 640 | 5.2 GiB | 7.5 GiB |
| 512 | 3.6 GiB | 5.2 GiB |
| 384 | 2.3 GiB | 3.3 GiB |
| 256 | always allowed | always allowed |

fp32 (which DirectML uses) doubles the per-pixel term, but the ladder never runs on DirectML anyway.

## 2. Setup

Work from a checkout that contains the four modified files (the change is uncommitted on the Linux dev box; apply the patch you were given first, then `git status` should list those four files as modified).

Use the MSYS2 shell the Makefile expects. Python lives at `.venv/Scripts/python.exe` on Windows.

**RTX 5080 laptop** (per `docs/releasing.md`, pass 1 — the 50-series needs cu128, the `cuda-legacy` wheel crashes on it):

```bash
make install
.venv/Scripts/pip install --force-reinstall "torch==2.11.0" torchvision --index-url https://download.pytorch.org/whl/cu128
```

**AMD 7900 XTX desktop** (torch-directml pins torch 2.4.1, which needs Python ≤ 3.12):

```bash
PY="py -3.12" GPU_VARIANT=directml make install
```

Sanity check on each machine and paste the output into the report:

```bash
.venv/Scripts/python -c "import torch; print(torch.__version__); print('cuda', torch.cuda.is_available()); print('bf16', torch.cuda.is_available() and torch.cuda.is_bf16_supported())"
```

On the AMD box also run:

```bash
.venv/Scripts/python -c "from proxy_scaler.upscale import resolve_device, device_backend; d=resolve_device(); print(d, device_backend(d))"
```

Expected: laptop prints `cuda True`, `bf16 True`. AMD prints a `privateuseone` device.

## 3. Unit tests (both machines)

```bash
.venv/Scripts/python -m pytest tests/test_oom.py tests/test_worker.py -q
```

Expected: `48 passed`. These use a fake CUDA device and need no GPU, so they must pass on the AMD box too. Any failure is a finding; paste it verbatim.

## 4. Scripts

Create these three files in the repo root (they are not part of the change; delete them when done). Run every script with `-u` so output isn't buffered, and always from the repo root so `weights/` is found. The first run of any script downloads model weights (~140 MB for UltraSharpV2).

### 4a. `val_auto.py` — the ladder across cards and under pressure

```python
"""Prints the tile the ladder picks for successive cards, then again while
this process holds extra VRAM, then for a light model."""
import sys, time
from proxy_scaler.worker import _configure_cuda_allocator
print("alloc conf:", _configure_cuda_allocator())
import torch
from PIL import Image
from proxy_scaler import upscale as U
from proxy_scaler.upscale import Upscaler, UpscaleModel, DEFAULT_TILE_SIZE

img = Image.effect_noise((745, 1040), 64).convert("RGBA")
GiB = 1024**3
hold_gib = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0

def card(label, model=UpscaleModel.ULTRASHARP_V2, tile=DEFAULT_TILE_SIZE):
    up = Upscaler(model=model, tile=tile, tile_auto=True, weights_dir="weights")
    t = time.time(); r = up.upscale(img)
    if torch.cuda.is_available(): torch.cuda.synchronize()
    free = up._probe_free_vram()
    peak = ""
    if torch.cuda.is_available():  # peak stats reset per card, so this is THIS card's pass
        peak = f" peak_resv={torch.cuda.max_memory_reserved()/GiB:.2f}G peak_alloc={torch.cuda.max_memory_allocated()/GiB:.2f}G"
    print(f"[{label}] tile={up.tile} device={r.device} dtype={r.dtype} {time.time()-t:.1f}s "
          f"headroom={U._current_headroom():.2f} free={'n/a' if free is None else f'{free/GiB:.2f}G'}{peak}")

card("card1 first-task headroom 2.0")
card("card2")
card("card3")
if hold_gib > 0 and torch.cuda.is_available():
    # Windows: the cu128 wheel ignores expandable_segments, so cards 1-3 leave
    # ~12 GiB in the allocator's cache; without this the hog lands in that
    # cache (or spills to system RAM) and creates no real pressure.
    torch.cuda.empty_cache()
    hog = torch.empty(int(hold_gib * GiB), dtype=torch.uint8, device="cuda")
    card(f"card4 with {hold_gib} GiB held")
    card(f"card5 with {hold_gib} GiB held")
    del hog; torch.cuda.empty_cache()
    card("card6 released")
card("lite", model=UpscaleModel.ULTRASHARP_V2_LITE, tile=0)
card("anime-fast", model=UpscaleModel.REALESRGAN_ANIME_FAST, tile=0)
```

### 4b. `val_oom.py` — force a real OOM and watch the retry

```python
"""Lies to the VRAM probe so the ladder picks a rung that cannot fit, forcing
a genuine CUDA OOM, then shows the retry recovering on the GPU.
Arg 1: GiB to hold (default 11). Arg 2: 'jump' to make the re-probe report
3 GiB so the retry jumps straight to 384 instead of stepping to 640."""
import sys
from unittest.mock import patch
from proxy_scaler.worker import _configure_cuda_allocator; _configure_cuda_allocator()
import torch
from PIL import Image
from proxy_scaler.upscale import Upscaler, UpscaleModel, DEFAULT_TILE_SIZE

img = Image.effect_noise((745, 1040), 64).convert("RGBA")
GiB = 1024**3
hold = float(sys.argv[1]) if len(sys.argv) > 1 else 11.0
jump = len(sys.argv) > 2 and sys.argv[2] == "jump"

Upscaler(model=UpscaleModel.ULTRASHARP_V2, tile=384, tile_auto=False, weights_dir="weights").upscale(img)  # warm
torch.cuda.empty_cache()  # see val_auto.py: the hold must land in real VRAM, not the allocator's cache
hog = torch.empty(int(hold * GiB), dtype=torch.uint8, device="cuda")
real_free, _ = torch.cuda.mem_get_info()
print(f"holding {hold} GiB; really free now: {real_free/GiB:.2f}G")
up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, tile=DEFAULT_TILE_SIZE, tile_auto=True, weights_dir="weights")
probes = [16 * GiB, 3 * GiB] if jump else [16 * GiB]
with patch.object(up, "_probe_free_vram", side_effect=lambda: probes.pop(0) if len(probes) > 1 else probes[0]):
    r = up.upscale(img)
print(f"final tile={up.tile} device={r.device} size={r.image.size}")
```

### 4c. `val_hold.py` — hold VRAM from a *separate* process (for the worker/client test)

```python
"""Holds N GiB of VRAM until Ctrl+C. Run in a second terminal."""
import sys, time, torch
n = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
hog = torch.empty(int(n * 1024**3), dtype=torch.uint8, device="cuda")
print(f"holding {n} GiB; Ctrl+C to release"); 
while True: time.sleep(1)
```

## 5. RTX 5080 laptop — test matrix

Close other GPU users first (browsers with hardware acceleration, Discord, games). Note what is still using VRAM from Task Manager → Performance → GPU.

### T1. Baseline picks

```bash
.venv/Scripts/python -u val_auto.py 2>&1 | tee t1.log
```

Expected:
- `alloc conf: expandable_segments:True`.
- `inference config: bf16, tile ...` lines from the app, one per card.
- With ~14 GiB free: card1 `tile=0` (untiled fits even at 2.0× if free ≥ 12.6 GiB); if the desktop is using more, card1 `tile=640` and cards 2–3 `tile=0`. Either is correct. Report which.
- `headroom` after card1 is **≈1.40** if expandable segments works, **≈2.0–2.4** if Windows ignored it. This number is the key result. It must not move after the `lite`/`anime-fast` cards (passes under 1 GiB don't calibrate).
- `lite` and `anime-fast`: `tile=0`, no ladder involvement.
- `peak_resv` vs `peak_alloc` on the UltraSharpV2 cards: report both. Expandable segments working ⇒ reserved within ~5% of allocated (untiled ≈ 6.5 G / 6.3 G). Not working ⇒ reserved roughly 2× allocated (≈ 10.8 G / 6.3 G untiled — on a 16 GB card that still fits, so the run succeeds either way; the ratio is the evidence).

Reference output from the Linux dev box (3080 Ti 12 GB) for the same script with `7` held:

```
[card1 first-task headroom 2.0] tile=640 ... headroom=1.40 free=11.12G
[card2] tile=0 ...
[card4 with 7.0 GiB held] tile=512 ... free=4.12G
[card6 released] tile=0 ...
[lite] tile=0 ...
```

Also grep the log for these and report presence/absence:

```bash
grep -n "expandable_segments not supported\|memory allocation failed\|memory mapping failed\|low free VRAM\|Upscale OOM" t1.log
```

`expandable_segments not supported on this platform` appearing once = Windows wheel ignores the setting (important, not a failure of the code — the calibrated headroom is the safety net in that case).

### T2. Pressure step-down (same process)

Run with increasing holds. Free VRAM will be roughly (total − desktop usage − hold).

```bash
.venv/Scripts/python -u val_auto.py 7 2>&1 | tee t2-7.log
.venv/Scripts/python -u val_auto.py 10 2>&1 | tee t2-10.log
.venv/Scripts/python -u val_auto.py 12 2>&1 | tee t2-12.log
.venv/Scripts/python -u val_auto.py 13.5 2>&1 | tee t2-13.log
```

Expected for cards 4–5 (steady-state headroom ≈1.4, ~15 GiB usable):

| hold | free ≈ | expected tile |
|---|---|---|
| 7 | 8 GiB | 640 |
| 10 | 5 GiB | 512 |
| 12 | 3 GiB | 384 (prints `low free VRAM: stepping tile down from 384 to ...` only when it goes below 384) |
| 13.5 | 1.5 GiB | 256, with the `low free VRAM` line |

Card 6 (released) must go back to `tile=0`. If your free numbers differ, judge against the gate table in §1 using the `free=` value the script prints, not the hold size. If headroom is stuck at ~2× (T1 said expandable segments is ignored), use the 2.0× column.

### T3. Real OOM → retry on GPU

**Windows and the sysmem fallback — this is now the main thing T3 tests.** The stock NVIDIA driver setting ("CUDA - Sysmem Fallback Policy" at its default) masks every CUDA OOM: the pass spills into system RAM and crawls (~15× slower) instead of raising, so the retry never fires. The code now caps torch's allocator per task at what is physically free (`Upscaler._cap_allocator_to_free`, via `torch.cuda.set_per_process_memory_fraction`), so the OOM should be raised *inside torch* before the driver is ever asked to spill. **Run T3 with the driver setting at its stock default first** — the retry lines must appear and the run must finish in ~15 s, not minutes. Only if that fails, set *Prefer No Sysmem Fallback*, rerun, and report both. Also report `Shared GPU memory` in Task Manager during the run: it should stay flat.

```bash
.venv/Scripts/python -u val_oom.py 11 2>&1 | tee t3.log
.venv/Scripts/python -u val_oom.py 11 jump 2>&1 | tee t3-jump.log
```

Expected (first run): `inference config: bf16, tile off`, then `Upscale OOM on cuda:0 at tile off; retrying at tile 640…` (possibly a second retry to 512 if ~4 GiB free is too tight), and `final tile=640` or `512` with `device=gpu`. **A `device=cpu` result, or a run that takes minutes, is a failure** — it means VRAM was not released between retries.

Expected (jump run): one retry line going straight to the rung that fits 3 GiB at the *current* headroom — `384` at 1.4× (Linux), `256` at the ~2.3× a Windows process calibrates to — and `final tile=<that> device=gpu`.

If the first attempt does *not* raise an OOM at all (no retry line, `final tile=0`, run takes far longer than ~10 s, Task Manager shows **Shared GPU memory** climbing), the sysmem fallback policy is still enabled — see the prerequisite above. Record that outcome too; it is the failure mode real users on stock drivers see.

### T4. Manual tile is untouched

```bash
.venv/Scripts/python -u -c "
from proxy_scaler.worker import _configure_cuda_allocator; _configure_cuda_allocator()
from PIL import Image
from proxy_scaler.upscale import Upscaler, UpscaleModel
up = Upscaler(model=UpscaleModel.ULTRASHARP_V2, tile=384, tile_auto=False, weights_dir='weights')
r = up.upscale(Image.effect_noise((745, 1040), 64).convert('RGBA')); print('tile', up.tile, r.device)"
```

Expected: `inference config: bf16, tile 384` and `tile 384 gpu`.

### T5. End to end through the real worker (optional but valuable)

Three terminals from the repo root:

```bash
make api-dev
```

```bash
make worker-dev
```

```bash
make frontend-dev
```

Open the printed Vite URL in a browser, generate ~5 cards at UltraSharpV2 / 1200 DPI with tile size 0 (auto). The worker terminal shows the `inference config` lines.

For a pressured batch the **order matters**: a warm worker keeps its ~7 GiB allocator arena between tasks and counts it as reusable, so a hold started afterwards neither fits nor changes the worker's pick (correct behaviour, verified on the dev box). On Linux: Ctrl+C the worker, start `val_hold.py 8` in a fourth terminal (a size below what is actually free), **then** restart `make worker-dev` and generate 3 more cards. **On Windows a cross-process hold does not survive**: WDDM pages the idle holder out to system RAM the moment the worker demands VRAM, free VRAM climbs back and the worker legitimately picks untiled. Treat the held batch as not applicable on Windows and rely on T2 (in-process pressure) instead. Then:

```bash
.venv/Scripts/python -c "
import sqlite3; c = sqlite3.connect('data/timing_debug.db')
for r in c.execute('select task_id, model, dtype, effective_tile, round(inference_s,1), status from task_timings order by task_id desc limit 12'): print(r)"
```

Expected: `effective_tile` 0 (or 640 for the very first card) for the unpressured cards; for the held batch the restarted worker's first card lands one rung lower than the rest (2.0× headroom until calibrated). Values other than 0/640/512/384/256 are a finding.

Reference from the dev box (12 GB, fresh worker behind a 6 GiB hold, ~5 GiB free): unpressured cards `640, 0, 0, 0, 0, 0`; held cards `384, 512`.

Note: the worker started by `make worker-dev` is the only path that sets the allocator variable automatically; the scripts above call the same helper explicitly. Kill the worker when done (Ctrl+C) so it doesn't keep the GPU.

## 6. AMD 7900 XTX — test matrix

The ladder is CUDA-only: `_apply_auto_tile` resets to the base and returns for any non-CUDA device, `_probe_free_vram` returns `None`, and the peak-memory calls are skipped. The point here is "same behaviour as before, no new exceptions".

### A1. Unit tests — §3.

### A2. Baseline

```bash
.venv/Scripts/python -u val_auto.py 2>&1 | tee a2.log
```

Expected: `inference config: fp32, tile 384` for the three UltraSharpV2 cards, `tile=384 device=gpu dtype=fp32 free=n/a headroom=2.00` (headroom never calibrates without CUDA — correct), `tile=0` for lite. **Known, pre-existing:** `anime-fast` fatally aborts the process on DirectML (`dml_tensor_desc.cc:80 Check failed: rank <= DML_TENSOR_DIMENSION_COUNT_MAX`, exit 127) regardless of tile — unrelated to this change and tracked separately. Expect the script to die at that card; everything before it counts. `alloc conf` prints but is irrelevant to DirectML. The `if torch.cuda.is_available()` blocks are skipped, so no hold/peak lines appear. Record per-card seconds.

### A3. Manual tile — T4 as-is (expect `fp32, tile 384`).

### A4. OOM behaviour (best effort)

DirectML has no `mem_get_info`, so `val_oom.py` cannot run there. Instead try to induce pressure by holding memory on the DirectML device in a second terminal:

```bash
.venv/Scripts/python -c "
import time, torch, torch_directml
d = torch_directml.device(); x = torch.empty(int(20 * 1024**3), dtype=torch.uint8, device=d)
print('holding 20 GiB'); time.sleep(600)"
```

then rerun A2. Report whatever happens: still `tile 384` on GPU, an `Upscale OOM ... retrying` line, a `Falling back to CPU upscale` line, or an exception. If an exception is raised, paste its full text — `_is_oom_error` matches on the words "out of memory"/"oom", and DirectML's wording is unverified.

## 7. Report template

```
Machine: <laptop 5080 | desktop 7900xtx>
torch: <version>   cuda available: <>   bf16: <>   device backend: <>
Unit tests: <48 passed | failures pasted below>

T1/A2 baseline:
  alloc conf line: <>
  card1..3 tiles: <>   headroom after card1: <>
  peak reserved / allocated: <> / <>
  grep hits (expandable_segments not supported / memory allocation failed / memory mapping failed): <>

T2 pressure (5080 only):
  hold 7 → free <> → tile <>
  hold 10 → free <> → tile <>
  hold 12 → free <> → tile <>
  hold 13.5 → free <> → tile <>
  released → tile <>

T3 OOM (5080 only):
  retry lines: <paste>
  final tile / device / wall time: <>
  jump run: <>
  sysmem fallback observed? <yes/no>   result with "Prefer No Sysmem Fallback": <>

T4/A3 manual tile: <>
T5 worker + timing DB (if run): <effective_tile values>
A4 DirectML pressure (AMD only): <>

Anything unexpected (full log excerpts): <>
```

Attach the `.log` files.
