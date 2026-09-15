# Windows validation results: five-rung auto tile ladder + allocator change

Reply to `windows-auto-tile-validation.md`. Run on the RTX 5080 laptop against
`e412e68` (includes your post-dry-run tweak). The AMD 7900 XTX desktop was not
available — see §6. One code change was made on the branch, uncommitted (§4).

## TL;DR

| question | answer |
|---|---|
| Does the Windows cu128 wheel honour `expandable_segments`? | **No.** `UserWarning: expandable_segments not supported on this platform`, once per process. Reserved is 1.98× allocated (12.43 G / 6.27 G untiled). |
| Does the calibrated headroom catch that? | Yes — settles at 2.28, every card in T1/T2 stayed on the GPU. |
| Does the ladder step down / recover under pressure? | Yes, once the test script is fixed for Windows (§5). |
| Does the OOM retry work? | **Yes**, verified with sysmem fallback disabled: two OOMs → two clean step-downs → GPU in 14 s. |
| Does your `_HEADROOM_CALIBRATION_MIN_ALLOCATED` tweak fix the light-model drift? | **Yes** (F6 resolved). |
| Is there anything else? | **Yes — F1.** The calibration ratchets the ladder down to 256 and freezes there for the life of the worker. Windows-only. Fixed on the branch with a one-guard change, tested on hardware. |

## 1. Environment

```
Machine: laptop, RTX 5080 Laptop GPU, 15.89 GiB, driver 616.64, ~1 GiB idle desktop use
HEAD:    e412e68 (branch feature/auto-tiling-improvements) — change was already committed,
         no patch applied
```

The laptop's venv was a DirectML build (torch 2.4.1, Python 3.12). Per the
Makefile ("a variant switch is a fresh venv, full stop") it was rebuilt with
`make reinstall` rather than the doc's `make install`, which would have left
orphaned directml wheels in site-packages. Both variants were exercised on this
one machine:

| variant | torch | python | cuda | bf16 | resolve_device |
|---|---|---|---|---|---|
| cuda (cu128) | `2.11.0+cu128` | 3.14 | True | True | `cuda:0` |
| directml | `2.4.1+cpu` | 3.12 | False | False | `privateuseone:0` |

Note the cu128 index lists 2.11.0 with cp310–cp314 wheels; it just sorts
lexicographically before 2.7.x, which is easy to misread as "not present".

## 2. Filled §7 template

```
Machine: laptop 5080 (16 GB) — AMD 7900xtx NOT tested, box unavailable
torch: 2.11.0+cu128  cuda available: True  bf16: True  backend: cuda:0
Unit tests: 48 passed on e412e68 (both variants); 49 with the §4 change

T1/A2 baseline:
  alloc conf line: expandable_segments:True   (set, ignored by the wheel)
  card1..3 tiles: 0 / 0 / 0     headroom after card1: 2.28
    after lite / anime-fast: 2.28 / 2.28   (unchanged — your tweak works)
  peak reserved / allocated: 12.43G / 6.27G  (1.98x)
  grep hits: "expandable_segments not supported on this platform" x1
             no real "memory allocation failed" (only the app's own note line)
             no "memory mapping failed"
  A2 directml: cards 384 fp32 gpu (36.3 / 33.3 / 33.3 s), headroom 2.00, free n/a
               lite tile 0 (4.8 s); anime-fast = FATAL ABORT, pre-existing (F4)

T2 pressure (5080):   [val_auto.py needed an empty_cache() fix first — F5]
  hold 7    -> card4 free 7.35G -> 512   card5 8.40G -> 640
  hold 10   -> card4 free 4.38G -> 384   card5 6.36G -> 512
  hold 12   -> card4 free 2.49G -> 256*  card5 3.87G -> 384
  hold 13.5 -> card4 free 1.81G -> 256*  card5 1.81G -> 256*
  released  -> tile 0 in all four runs
  * = "low free VRAM: stepping tile down from 384 to 256" printed — only below 384, as specified
  (free= is sampled after the pass; judge the trend, not each row against the gate table)

T3 OOM (5080):
  sysmem fallback observed? YES. Stock driver setting masks every OOM:
    no retry line, final tile=0 / gpu / 1m28s (normal ~6s); jump run 58s
  with "Prefer No Sysmem Fallback": PASS
    Upscale OOM on cuda:0 at tile off; retrying at tile 640
    Upscale OOM on cuda:0 at tile 640; retrying at tile 512
    final tile=512 device=gpu       wall 13.9s
  jump run: one retry straight to 256, final tile=256 device=gpu, 14.3s
    (doc predicted 384. At the measured headroom 2.39, 384 needs 3.42G vs the
     3G re-probe, so 256 is the correct rung. Your 384 assumed headroom 1.40.)

T4/A3 manual tile: PASS both — "bf16, tile 384" / "fp32, tile 384"
T5 worker + timing DB: unpressured 0,0,0,0,0 (matches doc);
  held batch INCONCLUSIVE — WDDM evicted the holder mid-run (F3)
A4 DirectML pressure: NOT RUN (no AMD box)

Anything unexpected: F1 (critical, fixed on branch), F3, F4, F5 — below.
```

## 3. Findings

### F1 — headroom calibration ratchets the ladder to 256 and freezes it. CRITICAL. Fixed in §4.

`max_memory_reserved()` is a process-wide high-water the allocator never
returns, and `reset_peak_memory_stats()` cannot lower it below what is
currently reserved. So for any pass smaller than an earlier one, `reserved`
describes the *earlier* pass's arena; the ratio is not a measurement of this
pass and inflates in proportion to how much bigger that earlier pass was.
Headroom climbs, the next card picks a smaller rung, which measures an even
worse ratio. With **14 GiB reported free throughout**, on `e412e68`:

```
[untiled   ] tile=  0  resv=12.43G alloc=6.27G  headroom=2.28
[spike->256] tile=256  resv=12.43G alloc=0.98G  headroom=2.28   <- your 1 GiB floor works here
[recover 1 ] tile=640  resv=12.43G alloc=3.66G  headroom=3.91
[recover 2 ] tile=512  resv=12.43G alloc=2.59G  headroom=5.52
[recover 3 ] tile=384  resv=12.43G alloc=1.63G  headroom=8.77
[recover 4+] tile=256  resv=12.43G alloc=0.98G  headroom=8.77   <- frozen
```

Your 1 GiB floor only catches the 256 rung (0.98 G allocated). 640/512/384
all allocate above it and keep calibrating, so the floor delays the descent by
three cards and changes where it freezes (8.77 instead of 14.63); it does not
prevent it.

Trigger is one transient dip below the untiled gate; after that it is
self-sustaining and survives the pressure going away. That dip is easy to hit:
on a 16 GB card at 1200 DPI the untiled gate sits just under actual free VRAM.
The pre-tweak T5 run tripped it unaided (`0, 0, 640, 512, 384` with nothing
held); the post-tweak run of the same five cards stayed at `0`. Same code
path — just whether the dip happened.

Seen in the real worker: after a pressure episode the worker stayed at 256 for
every subsequent card and never returned to untiled. Inference time barely
moves (5.5 s at 256 vs 6.2 s untiled) so it is invisible in the timing DB;
the cost is tile seams on every later card.

Windows-only, and downstream of F2: on Linux `expandable_segments` keeps
reserved within ~4% of allocated, so the arena never balloons and the ratio
stays honest.

### F2 — the Windows cu128 wheel ignores `expandable_segments`. Confirmed, not a defect.

Answers the doc's main open question. `_configure_cuda_allocator()` sets the
variable correctly; torch warns once and reserves 1.98× allocated anyway. The
calibrated headroom is the designed fallback and it holds — but the allocator
change delivers no benefit on Windows, and packaging notes should say so.

### F3 — VRAM pressure cannot be simulated reliably on Windows. Methodology.

Two mechanisms, both relevant to the doc's procedures:

- **In-process, the driver spills to shared memory** (CUDA sysmem fallback).
  This is what masked T3 — no OOM, ~15× slower instead. For users this is
  arguably worse than an OOM: no rung step-down, just a silent crawl.
  Disabling it in NVIDIA Control Panel (*CUDA — Sysmem Fallback Policy →
  Prefer No Sysmem Fallback*) makes T3 pass. The setting has been left
  **enabled** on the laptop for now — restore it when done with this work.
- **Cross-process, WDDM evicts the holder.** Your revised T5 (stop worker,
  start `val_hold.py 8`, restart worker) established 8 GiB, then the moment
  the worker demanded VRAM, Windows paged the idle holder out: process alive,
  GPU footprint down to ~4 GiB, 5.2 GB of its allocation resident in system
  RAM. Free VRAM rose back over the 9 GiB untiled floor mid-batch, so the
  fresh worker's first card legitimately picked `tile=0` rather than the
  predicted lower rung. Any procedure that relies on a second process
  pinning VRAM is unreliable on Windows.

The "warm worker keeps its ~7 GiB arena and counts it as reusable" note in
your revised T5 is correct and was observed too.

### F4 — `anime-fast` fatally aborts on DirectML. Pre-existing, not this change.

```
[F914 ...] dml_tensor_desc.cc:80] Check failed: rank <= DML_TENSOR_DIMENSION_COUNT_MAX
exit code 127
```

Identical abort at the parent commit `694165f` (checked in a throwaway
worktree) and with a manual `tile=384`, so it is a flat
`realesr-animevideov3` × DirectML incompatibility unrelated to tiling.
Consequences: §6 A2 can never reach its "no traceback" expectation on a
DirectML box, and a fatal abort takes the whole worker down rather than
surfacing as a failed task. Deserves its own ticket. The heavy models and
`lite` behave correctly on DirectML.

### F5 — `val_auto.py` cannot create real pressure on Windows. Tooling.

As written, T2 is invalid here: the hog is allocated after cards 1–3 have
left ~12.4 GiB in torch's cache, so it can't fit in real VRAM and spills.
Tell: `free=` pinned at `12.34G` for every hold, `peak_resv` 25.93 GiB on a
15.89 GiB card. Fix is one line before the hog:

```python
torch.cuda.empty_cache()  # drop the allocator cache so the hold lands in real VRAM
hog = torch.empty(int(hold_gib * GiB), dtype=torch.uint8, device="cuda")
```

Same fix needed in `val_oom.py` after the warm pass. The scripts work
unmodified on Linux only because expandable_segments keeps that cache small.
Suggest folding this into the doc for anyone re-running on Windows.

Caveat on T2 numbers generally: the in-process hog counts toward `allocated`,
so cards 4–6 report a flatteringly low headroom (1.40–1.57). Harness artifact;
the real worker has no hog. The F1 repro (no hog) is the cleaner evidence.

### F6 — light-model passes recalibrated headroom. RESOLVED by `e412e68`.

Pre-tweak: 2.28 → 1.66 (lite) → 1.63 (anime-fast). Post-tweak: flat at 2.28.
Both light passes allocate under 1 GiB (0.77 G, 0.38 G) and are skipped.
T1 now matches spec.

Minor: the §1 gate table is 4–8% more conservative than the estimator
actually computes (untiled 12.5 G vs 12.6 G stated; 640 → 7.1 G vs 7.5 G).
Cosmetic, picks unaffected.

## 4. Code change on the branch (uncommitted)

Rule: only calibrate from a pass that sets a new allocated high-water, because
only then does `reserved` correspond to it.

```diff
--- a/proxy_scaler/upscale.py
+++ b/proxy_scaler/upscale.py
@@ -501,13 +501,30 @@ def _current_headroom() -> float:
 # that would drag the heavy models' gate around for no reason.
 _HEADROOM_CALIBRATION_MIN_ALLOCATED = 1024**3

+# Largest allocated peak this process has seen. Reserved is a process-wide
+# high-water the allocator never returns (reset_peak_memory_stats() can't
+# lower it below what's currently reserved), so for a pass smaller than an
+# earlier one `reserved` describes the EARLIER pass's arena and the ratio
+# just scales with how much bigger that pass was. Verified live on Windows,
+# where expandable_segments is unsupported and the arena really does sit at
+# its high-water: after a 6.27 GiB untiled pass reserved 12.43 GiB, a
+# 3.66 GiB tile-640 pass measured 3.91x and a 1.63 GiB tile-384 pass 8.77x,
+# and the gate walked the ladder down to its 256 floor for good. Only a new
+# allocated peak re-measures something real.
+_PEAK_ALLOCATED_SEEN = 0
+

 def _record_observed_headroom(reserved: int, allocated: int) -> None:
     """Fold a pass's peak reserved/allocated ratio (x1.15 safety) into the
-    headroom later tasks gate on, never below the default 1.4x."""
-    global _OBSERVED_HEADROOM
+    headroom later tasks gate on, never below the default 1.4x. Only passes
+    that set a new allocated high-water calibrate -- see
+    _PEAK_ALLOCATED_SEEN for why a smaller pass's ratio is meaningless."""
+    global _OBSERVED_HEADROOM, _PEAK_ALLOCATED_SEEN
     if allocated < _HEADROOM_CALIBRATION_MIN_ALLOCATED:
         return
+    if allocated < _PEAK_ALLOCATED_SEEN:
+        return
+    _PEAK_ALLOCATED_SEEN = allocated
     _OBSERVED_HEADROOM = max(_VRAM_HEADROOM_DEFAULT, reserved / allocated * 1.15)
```

`tests/test_oom.py`: `_PEAK_ALLOCATED_SEEN` is patched to 0 alongside every
existing `_OBSERVED_HEADROOM` patch (it is module state and leaks between
tests otherwise); a `_MiB` constant added next to `_GiB`; and a new
`test_observed_headroom_ignores_passes_below_the_peak` covering "smaller
passes leave headroom alone, a new peak re-measures". Your existing
`test_observed_headroom_calibration` passes unchanged — its second call has
`allocated == peak`, and the guard is strict `<`.

Verification on hardware with the real patched code:

```
F1 repro (14 GiB free throughout):
  [untiled   ] tile=  0  alloc=6.27G  headroom=2.28
  [spike->256] tile=256  alloc=0.98G  headroom=2.28
  [recover 1-6] tile=640  alloc=3.66G  headroom=2.28     <- flat, no descent

Tight-start (does it still adapt upward?):
  [tight start ] tile=256  alloc=0.98G  headroom=2.00    <- default, nothing ≥1 GiB yet
  [freed up    ] tile=  0  alloc=6.27G  headroom=2.39    <- new peak, re-measures
  [recover 1-6 ] tile=640  alloc=3.66G  headroom=2.39    <- stable
```

T1/T2/T3 re-run on the patched code: unchanged / steps down and recovers at
every hold / retry passes. Full suite: 533 passed, 4 skipped (49 in
`test_oom.py` + `test_worker.py`).

Alternatives rejected: `empty_cache()` before every pass also works but pays
`cudaMalloc` on every card to fix a measurement bug; clamping headroom to a
ceiling bounds the runaway without correcting it. The guard is effectively a
no-op on Linux — every ratio lands near 1.04 and floors at 1.4 regardless of
pass size.

## 5. Suggested doc edits

- §4a/§4b: add the `empty_cache()` line before the hog (F5), or note that
  T2/T3 as written are Linux-only.
- §5 T3: promote the "Windows-specific" note to a prerequisite — with the
  stock driver setting the test cannot pass, and the run doesn't fail loudly,
  it just crawls.
- §5 T3 jump expectation: "384" holds only at headroom 1.40; on Windows
  (headroom ~2.3–2.4) the correct rung is 256. Phrase as "the rung that fits
  3 GiB at the current headroom".
- §5 T5 held batch: a cross-process hold does not survive on Windows (F3).
  No good replacement found; the in-process `val_auto2.py` with
  `empty_cache()` is the reliable pressure test here.
- §6 A2: `anime-fast` will abort on any DirectML box until F4 is fixed;
  either drop that card from A2 or list the abort as expected.

## 6. Not done

- **AMD 7900 XTX desktop**: A1–A4 not run on that machine. The DirectML rows
  above ran on the laptop's own DML backend, which exercises the same
  non-CUDA branch (`_probe_free_vram` → `None`, headroom never calibrates)
  but is not a sign-off for that box. The §4 change cannot affect DirectML —
  `_record_allocator_headroom` returns early for non-CUDA devices.
- NVIDIA *Sysmem Fallback Policy* on the laptop is currently set to
  *Prefer No Sysmem Fallback*; needs restoring.
- The laptop venv is now the cu128 build. DirectML restores with
  `PY="py -3.12" GPU_VARIANT=directml make reinstall`.

## 7. Artifacts

Logs (`t1.log`, `t2-{7,10,12,13.5}.log`, `t3.log`, `t3-jump.log`, `a2.log`),
the doc's three scripts, the Windows-corrected `val_auto2.py` /
`val_oom2.py`, and the F1 repros `val_ratchet.py` (pre-tweak),
`val_ratchet3.py` (post-tweak, 6 recovery passes), `val_fix_proto.py` /
`val_fix_proto2.py` (proposed fix by monkeypatch, both scenarios) are in the
session scratchpad under `validation-artifacts/`. A rendered version of this
report: https://claude.ai/code/artifact/f9a8a768-6d65-4c78-9c19-79cd8ef47d89
