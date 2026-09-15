# `realesrgan_anime_fast` fatally aborts on DirectML (F4)

Follow-up to F4 in `docs/windows-auto-tile-validation-results.md`. Two
changes on the branch, uncommitted: the root-cause workaround in
`proxy_scaler/upscale.py`, and crash-loop protection in
`proxy_scaler/db.py` + `proxy_scaler/supervisor.py`. Verified here by unit
tests (no DirectML hardware on the Linux box); §4 is the procedure to
close it out on the Windows machine.

## 1. Root cause: channel-wise `nn.PReLU` on torch-directml

```
[F914 ...] dml_tensor_desc.cc:80] Check failed: rank <= DML_TENSOR_DIMENSION_COUNT_MAX
exit code 127
```

The op is **`torch.nn.PReLU` with `num_parameters > 1`**. SRVGGNetCompact
(spandrel's `Compact` arch, the `realesr-animevideov3.pth` weights) is
17 × `PReLU(num_parameters=64)` between its convolutions. Neither DAT2
model nor RealPLKSR contains a PReLU at all (GELU / LeakyReLU), which is
why they were fine on the same box.

Evidence, strongest first:

1. **Independent reproduction on the identical stack.** A Microsoft Q&A
   report ("Native DirectML crash with torch.nn.PReLU(num_parameters > 1)
   (dml_tensor_desc.cc:80)") on torch `2.4.1+cpu` + torch-directml
   `0.2.5.dev240914` + Python 3.11/AMD RX 9070 XT — the exact versions the
   Makefile's `directml` variant pins — isolates it in a fresh venv:
   ReLU/LeakyReLU/SiLU/Sigmoid/Tanh and `PReLU(num_parameters=1)` work,
   `PReLU(num_parameters=2)` aborts the process before Python sees an
   exception. Same file, same line, same check as ours. No Microsoft
   response; torch-directml has had no release since 0.2.5.
2. **Elimination by the models that work.** The other suspects unique to
   Compact's forward pass are `PixelShuffle(4)` on a `[1, 48, H, W]`
   tensor and `F.interpolate(mode="nearest", scale_factor=4)`. RealPLKSR
   (`ultrasharp_v2_lite`) runs the *identical* `PixelShuffle(4)` on the
   identical `[1, 48, H, W]` shape and works on DirectML, so pixel_shuffle
   is cleared; nearest 2-D upsample is what every Stable Diffusion UNet
   does on torch-directml daily.
3. **The plugin binary itself** (`torch_directml-0.2.5.dev240914`
   wheel, `torch_directml_native.pyd`, inspected with `strings`):
   `_prelu_kernel` and `upsample_nearest2d.out` are registered natively
   on `PrivateUse1`, `pixel_shuffle` is not (it runs through torch's
   generic 6-D reshape/permute path, which the working models prove is
   fine). The check string and `dml_tensor_desc.cc` path are present;
   `DML_TENSOR_DIMENSION_COUNT_MAX` is DirectML's *legacy* 5-D limit (the
   8-D one is `..._MAX1`), so the plugin's PReLU kernel is building a
   >5-D descriptor internally when broadcasting per-channel slopes.
4. The two upstream GitHub issues with this exact message (#271 GPEN,
   #445 RIFE v3) are both models built on channel-wise PReLU; both were
   closed without a fix.

### Why the chosen fix is a model patch, not a CPU fallback or a version pin

- **A CPU fallback "for that op"** is impossible in-process: the failure
  is a native `CHECK`, so there is no exception to catch and no point at
  which to move one tensor to the CPU. The only safe place to act is
  *before the first forward pass*.
- **A version pin** has nothing to pin to. torch-directml 0.2.5 is the
  newest release and hard-pins torch 2.4.1; older plugins are the same
  code with fewer ops.
- **A model-level "unsupported on privateuseone" guard** would work but
  gives up the GPU for no reason: `prelu(x) = relu(x) + w·min(0, x)`
  is exactly `relu(x) − w·relu(−x)`, and relu / neg / broadcast-mul /
  sub are ops the other models already exercise on DirectML.

So `_load_model` now runs `apply_directml_prelu_policy()` on the loaded
model, *before* `.to(device)`, only when the device is `privateuseone`
and the model contains a channel-wise PReLU:

| `PROXY_SCALER_DIRECTML_PRELU` | behaviour |
|---|---|
| `patch` (default) | swap each channel-wise `nn.PReLU` for `DirectMLSafePReLU` (same weight Parameter, so the state dict, `.to()` and the OOM CPU relocation are unaffected); stay on the GPU |
| `cpu` | leave the model alone and load it on the CPU instead (escape hatch if the patched path still misbehaves) |
| `off` | do nothing — reproduces the abort, for verification only |

Every other backend (cuda/ROCm/mps/cpu) and every PReLU-free model is
untouched regardless of the variable. The CPU route deliberately does
**not** fire `on_cpu_fallback` — that raises the client's "GPU ran out
of memory, cancel the queue?" dialog, and this is not an OOM.

Unit tests (`tests/test_directml_prelu.py`): the replacement equals
`nn.PReLU` to 1e-6 on random channel-wise slopes (and exactly at 0), the
patcher swaps exactly the 3 PReLU layers of a small `Compact` and leaves
the output bit-identical, single-parameter PReLU and PReLU-free models
are untouched, each policy routes correctly, and `_load_model` applies
the policy before the device move. Also checked once by hand on the real
`realesr-animevideov3.pth` on CPU: 17 layers swapped, max abs diff 0.0.

**Desktop client:** no change needed. `recommendedDefaultModel()` already
recommends the heavy model on `privateuseone`, and with the patch the
fast model works there too, so `/api/device` and the dropdown keep
offering it.

## 2. Robustness: a crashing task no longer takes the server down for good

Before:

1. The worker aborted; its row stayed `running`.
2. `supervisor.py` saw the child exit and **shut the API server down
   too** ("worker exited unexpectedly (code 127); shutting down"). In the
   desktop app that is "the server died".
3. Next launch: `reset_orphaned_running_tasks` re-queued the row →
   resume → the same task killed the new worker → 2. A crash loop, one
   app launch per iteration, with nothing in the Tasks tab explaining it.

Now:

- **`generation_tasks.attempts`** (migration 009) counts claims;
  `claim_next_task` increments it. `reset_orphaned_running_tasks` fails
  (instead of re-queuing) any orphan already claimed
  `MAX_TASK_ATTEMPTS = 2` times, with the error
  *"Worker process died while running this task 2 times in a row; not
  retried automatically. Use Retry to try again."* One automatic retry
  still covers a worker killed by an app close or power loss. A manual
  Retry keeps the count, so it buys exactly one more try.
- **The supervisor recovers instead of dying.** On an unexpected worker
  exit it calls `_recover_from_worker_crash`: if no worker holds the
  lock and there is a `running` row, that row is failed with
  *"Worker process crashed (exit code N) while running this task"* and a
  replacement worker is spawned (the API server never goes away). Bounded
  by `WORKER_MAX_RESPAWNS = 3` per supervisor lifetime; a worker that
  dies with nothing running (startup/idle failure) still shuts everything
  down as before, since a respawn can't fix that.

Tests: `tests/test_db.py` (attempts count, bounded orphan reset, the
fail helper, migration 8→9) and `tests/test_supervisor.py` (recovery
decision table, plus a live Linux test that SIGKILLs the worker mid-task
and checks the task failed with `exit code -9`, a replacement worker
appeared, and the API child survived).

## 3. Files touched

- `proxy_scaler/upscale.py` — DirectML PReLU section + `_load_model` hook
- `proxy_scaler/db.py` — `attempts` column/migration 009,
  `fail_running_tasks`, `reset_orphaned_running_tasks` → `OrphanReset`
- `proxy_scaler/supervisor.py` — `_recover_from_worker_crash`, respawn
- `proxy_scaler/worker.py` — reports abandoned orphans
- `tests/test_directml_prelu.py` (new), `tests/test_db.py`,
  `tests/test_supervisor.py`, `tests/test_customs.py` (schema-version
  assert)
- `docs/scripts/directml_prelu_probe.py` (new) — §4's per-op probe

## 4. Windows verification procedure (RTX 5080 laptop, directml venv)

```
PY="py -3.12" GPU_VARIANT=directml make reinstall      # restore the directml venv
python -m pytest tests -q                              # expect all green
```

**Step 1 — pin the op (each probe is its own subprocess, so an abort is
just a reported exit code):**

```
python docs/scripts/directml_prelu_probe.py
```

Expected:

```
prelu_channelwise        ABORT (exit 3221226505)   <- confirms the root cause
prelu_single             ok
prelu_safe_channelwise   ok                        max abs diff vs CPU nn.PReLU: 0.0
pixel_shuffle            ok
interpolate_nearest      ok
compact_native           ABORT (exit 3221226505)   <- the old worker behaviour
compact_patched          ok                        swapped 17 PReLU layers, max abs diff ~4e-6
```

3221226505 is 0xC0000409 (Windows fail-fast) as a bare interpreter reports
it; the frozen sidecar reported the same abort as exit 127.

**Step 1 result, 2026-09-14, RTX 5080 laptop** (torch `2.4.1+cpu`,
torch-directml `0.2.5.dev240914`, Python 3.12.10): all seven verdicts
exactly as above. The real `Upscaler.upscale()` path was also run
directly in the same venv: `ultrasharp_v2_lite` and the patched
`realesrgan_anime_fast` both completed on `privateuseone:0`, and the
anime-fast output under the default `patch` policy matched the `cpu`
policy's output to within 1/255 (mean abs diff 0.0). The probe as first
written reported both full-model cases as `RuntimeError: Cannot set
version_counter for inference tensor` instead: on this plugin build every
conv2d fails when the parameters were moved to the device *outside*
`torch.inference_mode()` but the activations are inference tensors
(spandrel's `ImageModelDescriptor.__call__` is `@torch.inference_mode()`).
The worker never hits this only because `upscale()` enters
`inference_mode()` before `_ensure_model()` moves the model, so the probe
now does the same. Anyone restructuring `upscale()` so the model move
happens outside that context will break every model on DirectML, not
just this one.

If `prelu_safe_channelwise` or `compact_patched` aborts, the workaround
is wrong for this plugin build: set `PROXY_SCALER_DIRECTML_PRELU=cpu`
for the worker (Step 3) and report which probe failed.

**Step 2 — the worker, default policy:** queue 3 anime-fast cards
(tile 0) plus 1 at manual tile 384 from the app. Expect in the worker
log, once, at model load:

```
  DirectML: swapped 17 channel-wise PReLU layer(s) for the relu-based equivalent ...
Loading realesrgan_anime_fast x4 on privateuseone:0 (realesr-animevideov3.pth)...
  inference config: fp32, tile off
```

and every card `done` with device `gpu` in the Tasks tab. Eyeball one
output against a CUDA-generated one of the same card.

**Step 3 — the escape hatch:** restart the server with
`PROXY_SCALER_DIRECTML_PRELU=cpu` set, regenerate one card: log says
*"running it on the CPU instead"*, Tasks shows device `cpu`, and the
"GPU ran out of memory" dialog does **not** appear.

**Step 4 — the crash-loop guard, using the abort as the crash:** restart
with `PROXY_SCALER_DIRECTML_PRELU=off`, queue 2 anime-fast cards.
Expect:

1. Worker aborts on card 1. Supervisor stderr:
   `worker exited unexpectedly (code 127) mid-task; failed 1 running
   task(s) and restarting it (1/3).` The app stays connected (no "server
   died"); card 1 shows `failed` with *"Worker process crashed (exit code
   127) while running this task"*.
2. The replacement worker claims card 2 and aborts → `(2/3)`, card 2
   failed the same way. Queue empty; the third replacement worker sits
   idle. API still answering.
3. Retry both cards (still `off`): the crash costs `(3/3)`, then the
   4th death shuts the server down as before — the bound is
   intentional. Relaunch the app: the held-worker prompt appears, resume,
   and the orphan is failed at startup rather than re-queued (worker
   stderr: `Failed 1 task(s) that already killed 2 worker(s) in a row`).
   No further crash.

Unset the variable afterwards. Step 4 also exercises everything on the
CUDA venv if you `os.kill` the worker mid-task instead of using the
abort — the live Linux test does exactly that.

## Sources

- Microsoft Q&A: Native DirectML crash with torch.nn.PReLU(num_parameters > 1)
  (dml_tensor_desc.cc:80) — https://learn.microsoft.com/en-us/answers/questions/5971615/native-directml-crash-with-torch-nn-prelu-num-para
- microsoft/DirectML#445 (RIFE v3, closed not planned) — https://github.com/microsoft/DirectML/issues/445
- microsoft/DirectML#271 (GPEN) — https://github.com/microsoft/DirectML/issues/271
- DirectML constants (`DML_TENSOR_DIMENSION_COUNT_MAX` = 5, `..._MAX1` = 8) — https://learn.microsoft.com/en-us/windows/win32/direct3d12/direct3d-directml-constants
