"""The Vulkan (ncnn) inference backend — the second runtime behind
UpscaleModel.backend == Backend.NCNN.

Why it exists: the torch path reaches AMD GPUs on Linux only through ROCm,
and on at least one RDNA3 card (RX 7600 XT, gfx1102) ROCm corrupts the
display and returns black images while Vulkan on the same machine runs
clean (Upscayl works there). ncnn runs its models on any Vulkan device —
AMD, Nvidia, Intel, Apple via MoltenVK — so a "Vulkan Models" group gives
every user a GPU path that doesn't depend on their vendor's compute stack.

Shape: NcnnUpscaler mirrors upscale.Upscaler's public surface (constructor
kwargs, model_id / scale / tile, upscale() -> UpscaleResult) so
pipeline._upscalers_for_targets can pick either by model.backend and the
rest of the pipeline never knows which ran. No torch anywhere in here; ncnn
itself is imported lazily for the same reason upscale.py defers torch (this
module is on the API process's import path).

What the Python binding can't do, and how that's handled:

- No VRAM figure (GpuInfo exposes name/type only), so tiles come from the
  user's VRAM tier (upscale.NCNN_TILE_PRESETS) and a failed pass steps
  down the presets before falling to the CPU.
- A pass can fail *silently*: on this dev box ncnn's CPU path returned
  garbage (max 1e6, or values scaled to 0.78) on some fresh nets and
  clean output on the next — and the whole reason this backend exists is
  a GPU that returned black without an error. So every result goes
  through _plausible(): finite, and its 4x box-downscale must sit within
  ~20 dB of the input (a real upscale lands at ~30 dB, black at ~10,
  garbage at ~11). Implausible output is treated exactly like a raised
  error: next rung of the ladder.
- fp16 on the CPU path produced NaN outright, so CPU nets always run
  fp32; GPU nets run fp16 first and retry fp32 at the same tile if the
  fp16 result is implausible (the torch path's bf16->fp32 guard, same idea).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from .upscale import (
    NCNN_TILE_PRESETS,
    Backend,
    UpscaleModel,
    UpscaleResult,
    default_tile_preset,
    ensure_weight_files,
    parse_model,
)

if TYPE_CHECKING:
    import ncnn  # noqa: F401

# Environment knobs (all optional):
#   PROXY_SCALER_VULKAN_GPU   integer ncnn device index; -1 forces the CPU path
#   PROXY_SCALER_VULKAN_TILE  tile size in px, overrides the client's choice
VULKAN_GPU_ENV = "PROXY_SCALER_VULKAN_GPU"
VULKAN_TILE_ENV = "PROXY_SCALER_VULKAN_TILE"

# ncnn's Vulkan device types (GpuInfo.type): 0 discrete, 1 integrated,
# 2 virtual, 3 cpu (Mesa's llvmpipe shows up as a "GPU" of type 3 —
# software rasterisation, slower than the plain CPU path and pointless).
_DEVICE_TYPE_DISCRETE = 0
_DEVICE_TYPE_INTEGRATED = 1
_DEVICE_TYPE_CPU = 3

# Input/output blob names every model we ship uses (the converter and the
# official Real-ESRGAN ncnn files both follow realesrgan-ncnn-vulkan's
# convention).
INPUT_BLOB = "data"
OUTPUT_BLOB = "output"

# See module docstring: a genuine 4x upscale box-downsampled back to the
# input resolution sits ~30 dB above it; black/garbage sits at ~10-11 dB.
PLAUSIBILITY_MIN_PSNR_DB = 20.0
# ...and the ceiling for "the output is just the input blown up" (see
# identity_psnr): real model outputs sit far below this.
IDENTITY_MAX_PSNR_DB = 50.0
# ncnn reports Vulkan failures on stderr and carries on with whatever
# buffers it has; these are the lines that mean the pass can't be trusted.
import re as _re

_VK_ERROR_RE = _re.compile(r"\bvk\w*\s+failed\b|out of (device )?memory|VK_ERROR", _re.IGNORECASE)

# The rung below the smallest preset is the CPU. A pass there is retried
# once before giving up, because the failure mode observed on the CPU
# path was a first-run garbage result on an otherwise fine net.
_CPU_ATTEMPTS = 2

# Bundled MoltenVK location on macOS (ncnn dlopens libMoltenVK.dylib by
# bare name; the wheel doesn't ship it). Frozen: inside the onedir
# bundle's _internal/ncnn-vulkan/. Dev: tools/molten-vk/ (gitignored,
# `make molten-vk`).
_MOLTENVK_BUNDLE_SUBDIR = "ncnn-vulkan"
_MOLTENVK_NAME = "libMoltenVK.dylib"


class NcnnUnavailable(RuntimeError):
    """ncnn can't be imported at all — a packaging failure, not a GPU one."""


# --- runtime discovery ------------------------------------------------------

_MOLTENVK_PRELOADED: bool | None = None


def _moltenvk_candidates() -> list[Path]:
    paths: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass) / _MOLTENVK_BUNDLE_SUBDIR / _MOLTENVK_NAME)
    paths.append(Path(__file__).resolve().parents[1] / "tools" / "molten-vk" / _MOLTENVK_NAME)
    return paths


def _preload_moltenvk() -> bool:
    """macOS only: dlopen our bundled MoltenVK by absolute path before ncnn
    asks for it by bare name, so dyld resolves the latter against the
    already-loaded image. Harmless elsewhere and when nothing is bundled
    (ncnn then simply reports no GPU and the CPU path runs)."""
    global _MOLTENVK_PRELOADED
    if _MOLTENVK_PRELOADED is not None:
        return _MOLTENVK_PRELOADED
    _MOLTENVK_PRELOADED = False
    if sys.platform != "darwin":
        return False
    for candidate in _moltenvk_candidates():
        if candidate.is_file():
            try:
                ctypes.CDLL(str(candidate))
                _MOLTENVK_PRELOADED = True
                break
            except OSError as exc:
                print(f"warning: could not preload {candidate}: {exc}", file=sys.stderr)
    return _MOLTENVK_PRELOADED


def _ncnn():
    """The ncnn module, imported on first use (see module docstring)."""
    _preload_moltenvk()
    try:
        import ncnn
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise NcnnUnavailable(
            "the ncnn package is not installed — Vulkan models need it "
            "(pip install ncnn; it is a declared dependency)"
        ) from exc
    return ncnn


@dataclass(frozen=True)
class VulkanDevice:
    index: int
    name: str
    type: int


def list_vulkan_devices() -> list[VulkanDevice]:
    """Every Vulkan device ncnn can see, including software ones. Never
    raises: an absent loader / driver is an empty list."""
    try:
        ncnn = _ncnn()
        count = int(ncnn.get_gpu_count())
        out = []
        for i in range(count):
            info = ncnn.get_gpu_device(i).info()
            out.append(VulkanDevice(i, str(info.device_name()), int(info.type())))
        return out
    except Exception:  # noqa: BLE001
        return []


def _is_real_gpu(dev: VulkanDevice) -> bool:
    if dev.type == _DEVICE_TYPE_CPU or "llvmpipe" in dev.name.lower():
        return False
    return dev.type in (_DEVICE_TYPE_DISCRETE, _DEVICE_TYPE_INTEGRATED)


_SELECTED_DEVICE: tuple[bool, int | None] = (False, None)


def _enumerate_with_loader_errors() -> tuple[list[VulkanDevice], list[str]]:
    """list_vulkan_devices(), with the Vulkan loader's own error lines
    captured. The loader is silent about drivers it skips unless
    VK_LOADER_DEBUG asks, so it's set to "error" for this first
    enumeration only (ncnn creates its instance here, once per process)
    and the captured lines are kept for the "none usable" message. A user
    setting of VK_LOADER_DEBUG is left alone."""
    had = "VK_LOADER_DEBUG" in os.environ
    if not had:
        os.environ["VK_LOADER_DEBUG"] = "error"
    try:
        with _StderrCapture(echo=False) as captured:
            devices = list_vulkan_devices()
    finally:
        if not had:
            os.environ.pop("VK_LOADER_DEBUG", None)
    errors = [
        " ".join(line.split())
        for line in captured.text.splitlines()
        if "ERROR" in line or "not found" in line
    ]
    return devices, errors


def select_vulkan_device() -> int | None:
    """The device index Vulkan passes run on, or None for the CPU path.
    Env override first, else the first discrete GPU, else the first
    integrated one. Decided once per process and logged once."""
    global _SELECTED_DEVICE
    if _SELECTED_DEVICE[0]:
        return _SELECTED_DEVICE[1]
    chosen: int | None = None
    devices, loader_errors = _enumerate_with_loader_errors()
    override = (os.environ.get(VULKAN_GPU_ENV) or "").strip()
    if override:
        try:
            idx = int(override)
        except ValueError:
            idx = None
            print(f"warning: {VULKAN_GPU_ENV}={override!r} is not an integer; ignoring", file=sys.stderr)
        if idx is not None and idx >= 0 and any(d.index == idx for d in devices):
            chosen = idx
        elif idx is not None and idx >= 0:
            print(f"warning: {VULKAN_GPU_ENV}={idx} is not a Vulkan device here; using auto", file=sys.stderr)
            idx = None
        if idx == -1:
            chosen = None
            print("  vulkan device: CPU (forced by env)")
            _SELECTED_DEVICE = (True, None)
            return None
    if chosen is None and not override.startswith("-"):
        real = [d for d in devices if _is_real_gpu(d)]
        real.sort(key=lambda d: (d.type != _DEVICE_TYPE_DISCRETE, d.index))
        if real:
            chosen = real[0].index
    if chosen is None:
        print("  vulkan device: none usable — Vulkan models will run on the CPU")
        # Say why, when the loader told us: a driver that exists but won't
        # load (wrong libstdc++, missing dependency) is invisible otherwise.
        for line in loader_errors[:6]:
            print(f"  vulkan loader: {line}")
    else:
        dev = next(d for d in devices if d.index == chosen)
        print(f"  vulkan device: {dev.name} (index {dev.index}, type {dev.type})")
    _SELECTED_DEVICE = (True, chosen)
    return chosen


def vulkan_available() -> bool:
    """Whether a real (non-software) Vulkan GPU is usable — the capability
    bit GET /api/device reports. Cheap after the first call."""
    return select_vulkan_device() is not None


def describe_vulkan_device() -> str:
    idx = select_vulkan_device()
    if idx is None:
        return "cpu"
    dev = next((d for d in list_vulkan_devices() if d.index == idx), None)
    return f"{dev.name} via Vulkan" if dev else f"vulkan device {idx}"


# --- model handles ----------------------------------------------------------


class _NcnnNet:
    """One loaded .param/.bin on one device at one precision."""

    def __init__(self, param: Path, weights: Path, gpu: int | None, fp16: bool) -> None:
        ncnn = _ncnn()
        self.gpu = gpu
        self.fp16 = fp16 if gpu is not None else False
        net = ncnn.Net()
        opt = net.opt
        opt.use_vulkan_compute = gpu is not None
        opt.use_fp16_storage = self.fp16
        opt.use_fp16_arithmetic = self.fp16
        opt.use_fp16_packed = self.fp16
        # bf16 storage is a CPU-only fast path that returned NaN with
        # these models; keep every path on plain fp32 storage there.
        opt.use_bf16_storage = False
        if gpu is not None:
            net.set_vulkan_device(gpu)
        if net.load_param(str(param)) != 0:
            raise RuntimeError(f"ncnn could not parse {param.name}")
        if net.load_model(str(weights)) != 0:
            raise RuntimeError(f"ncnn could not load {weights.name}")
        self._net = net

    def run(self, chw: np.ndarray) -> np.ndarray:
        """float32 CHW in [0,1] -> float32 CHW at the model's scale."""
        ncnn = _ncnn()
        # ncnn.Mat(ndarray) wraps the array's memory, it does not copy it.
        # Both the contiguous buffer and the Mat must outlive extract():
        # built inline as a temporary, the buffer was freed as soon as the
        # expression ended and the engine read released memory — NaN on
        # fp16, 1e18-sized pixels on fp32, and different answers on
        # identical repeat runs. Tile crops are non-contiguous views, so
        # this copy happens on every call.
        buffer = np.ascontiguousarray(chw, dtype=np.float32)
        mat_in = ncnn.Mat(buffer)
        ex = self._net.create_extractor()
        ex.input(INPUT_BLOB, mat_in)
        ret, mat_out = ex.extract(OUTPUT_BLOB)
        if ret != 0:
            raise RuntimeError(f"ncnn extract failed (code {ret})")
        # np.array() copies, so the result is ours once mat_out goes away.
        out = np.array(mat_out, dtype=np.float32)
        del mat_out, mat_in, buffer
        return out


_NET_CACHE: dict[tuple, _NcnnNet] = {}


def clear_ncnn_cache() -> None:
    """Drop every loaded net (and with it its device memory)."""
    _NET_CACHE.clear()


def _cache_key(model: UpscaleModel, scale: int, weights_dir: Path, gpu: int | None, fp16: bool) -> tuple:
    return (model, scale, str(Path(weights_dir).resolve()), gpu, fp16)


# --- the plausibility gate --------------------------------------------------


def _box_downscale(chw: np.ndarray, factor: int) -> np.ndarray:
    c, h, w = chw.shape
    h2, w2 = h // factor, w // factor
    return chw[:, : h2 * factor, : w2 * factor].reshape(c, h2, factor, w2, factor).mean(axis=(2, 4))


def consistency_psnr(output: np.ndarray, source: np.ndarray, scale: int) -> float:
    """PSNR between the output box-downscaled by `scale` and the source.
    Both float32 CHW in [0,1]; output must be exactly scale x source."""
    down = _box_downscale(np.clip(output, 0.0, 1.0), scale)
    h = min(down.shape[1], source.shape[1])
    w = min(down.shape[2], source.shape[2])
    mse = float(np.mean((down[:, :h, :w] - source[:, :h, :w]) ** 2))
    if mse <= 0.0:
        return 99.0
    return float(10.0 * np.log10(1.0 / mse))


def _nearest_upscale(source: np.ndarray, scale: int) -> np.ndarray:
    return np.repeat(np.repeat(source, scale, axis=1), scale, axis=2)


def identity_psnr(output: np.ndarray, source: np.ndarray, scale: int) -> float:
    """PSNR between the output and a plain nearest-neighbour blow-up of the
    source. A working model always departs from that (typically 20-35 dB
    on a card); a result that *matches* it did no work at all."""
    nn = _nearest_upscale(source, scale)
    mse = float(np.mean((np.clip(output, 0.0, 1.0) - nn) ** 2))
    if mse <= 0.0:
        return 99.0
    return float(10.0 * np.log10(1.0 / mse))


def _plausible(output: np.ndarray, source: np.ndarray, scale: int) -> bool:
    if output.shape != (source.shape[0], source.shape[1] * scale, source.shape[2] * scale):
        return False
    if not np.isfinite(output).all():
        return False
    if consistency_psnr(output, source, scale) < PLAUSIBILITY_MIN_PSNR_DB:
        return False
    # Seen live on a GPU with ~1 GB of VRAM left: every vkAllocateMemory
    # failed, the convolution branch silently produced zeros, and the
    # model's bypass path handed back the input nearest-neighbour
    # upscaled — finite, "consistent" with the input to 99 dB, and
    # useless. That is what an unchanged image looks like, so treat it
    # as a failed pass.
    if identity_psnr(output, source, scale) > IDENTITY_MAX_PSNR_DB:
        return False
    return True


class _StderrCapture:
    """Temporarily divert fd 2 into a file so ncnn's C++-side errors
    (NCNN_LOGE -> stderr: "vkAllocateMemory failed -2", "vkQueueSubmit
    failed", ...) can be read back after a pass. Python-level redirection
    can't see them; only the descriptor can. Everything captured is
    re-emitted to the real stderr afterwards so the worker log stays
    complete. Best-effort: with no usable fd 2 (a windowless frozen
    process) capture is skipped and `text` stays empty."""

    def __init__(self, echo: bool = True) -> None:
        self.text = ""
        self._echo = echo
        self._saved: int | None = None
        self._tmp = None

    def __enter__(self) -> "_StderrCapture":
        import tempfile

        try:
            sys.stderr.flush()
            self._tmp = tempfile.TemporaryFile(mode="w+b")
            self._saved = os.dup(2)
            os.dup2(self._tmp.fileno(), 2)
        except Exception:  # noqa: BLE001
            self._saved = None
        return self

    def __exit__(self, *exc) -> None:
        if self._saved is None:
            return
        try:
            sys.stderr.flush()
            os.dup2(self._saved, 2)
            os.close(self._saved)
            self._tmp.seek(0)
            self.text = self._tmp.read().decode("utf-8", "replace")
            self._tmp.close()
            if self.text and self._echo:
                sys.stderr.write(self.text)
                sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass


def _vulkan_error_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if _VK_ERROR_RE.search(line)]


# --- the upscaler -----------------------------------------------------------

# Once a task has fallen all the way to the CPU, later tasks in this
# process skip the GPU rungs: the failure was almost certainly the device,
# and re-failing four rungs per card is a poor way to find that out again.
_CPU_ONLY_REASON: str | None = None


def resolve_ncnn_tile(tile_setting: int) -> int:
    """The tile a Vulkan pass starts at: env override, else the client's
    explicit number (a preset's tile, normally), else the default preset.
    Never 0 — an untiled pass is not a rung on this ladder."""
    override = (os.environ.get(VULKAN_TILE_ENV) or "").strip()
    if override:
        try:
            value = int(override)
            if value > 0:
                return value
        except ValueError:
            pass
        print(f"warning: {VULKAN_TILE_ENV}={override!r} ignored", file=sys.stderr)
    if tile_setting > 0:
        return tile_setting
    preset = default_tile_preset(UpscaleModel.REALESRGAN_ANIME_FAST_VK)
    return preset.tile if preset else NCNN_TILE_PRESETS[0].tile


def _tile_ladder(start: int) -> list[int]:
    """`start`, then every preset tile below it, largest first."""
    rungs = [start] + [p.tile for p in NCNN_TILE_PRESETS if p.tile < start]
    return sorted(set(rungs), reverse=True)


class NcnnUpscaler:
    """Vulkan-backed twin of upscale.Upscaler for Backend.NCNN models."""

    def __init__(
        self,
        model: UpscaleModel | str,
        scale: int = 4,
        weights_dir: Path | str = "weights",
        tile: int = 0,
        tile_pad: int = 32,
        timings: object | None = None,
        tile_auto: bool = False,
        on_cpu_fallback: object | None = None,
    ) -> None:
        self.model_id = parse_model(model)
        if self.model_id.backend is not Backend.NCNN:
            raise TypeError(
                f"{self.model_id.value} runs on the {self.model_id.backend.value} "
                "backend; construct it via make_upscaler() (or Upscaler)"
            )
        if scale not in self.model_id.supported_scales:
            raise ValueError(
                f"{self.model_id.value} supports scales "
                f"{self.model_id.supported_scales}, not x{scale}"
            )
        self.scale = scale
        self.weights_dir = Path(weights_dir)
        # The rung this task starts at; updated to whatever rung succeeded
        # so the pipeline records the tile that actually ran.
        self._base_tile = resolve_ncnn_tile(tile)
        self.tile = self._base_tile
        self.tile_pad = tile_pad
        self.tile_auto = tile_auto
        self._timings = timings
        self._on_cpu_fallback = on_cpu_fallback

    # -- plumbing shared with Upscaler (duck-typed, see upscale.py) --------

    def _phase(self, name: str):
        if self._timings is not None:
            return self._timings.phase(name)  # type: ignore[attr-defined]
        return contextlib.nullcontext()

    def _notify_cpu_fallback(self) -> None:
        if self._on_cpu_fallback is None:
            return
        try:
            self._on_cpu_fallback()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001
            print(f"warning: cpu-fallback hook failed: {exc}", file=sys.stderr)

    # -- model loading ----------------------------------------------------

    def _net(self, gpu: int | None, fp16: bool) -> _NcnnNet:
        key = _cache_key(self.model_id, self.scale, self.weights_dir, gpu, fp16)
        net = _NET_CACHE.get(key)
        if net is not None:
            return net
        with self._phase("model_load"):
            # One slot across both runtimes: a torch model left warm in
            # this process keeps its allocator arena (11 GB observed for
            # an idle UltraSharpV2) and starves Vulkan of device memory —
            # ncnn then fails every allocation and returns the input
            # unchanged. Same eviction the torch models apply to each
            # other (upscale._cache_put), mirrored here.
            from . import upscale as _upscale

            _upscale.clear_model_cache()
            paths, _ = ensure_weight_files(self.model_id, self.scale, self.weights_dir)
            param, weights = paths[0], paths[1]
            where = "cpu" if gpu is None else f"vulkan:{gpu}"
            print(
                f"Loading {self.model_id.value} x{self.scale} on {where} "
                f"({'fp16' if fp16 and gpu is not None else 'fp32'}, {param.name})..."
            )
            net = _make_net(param, weights, gpu, fp16)
        # One slot per (device, precision): the fp32 twin of a GPU net is
        # a separate entry so the fp16 one stays warm after a retry.
        _NET_CACHE[key] = net
        return net

    # -- inference --------------------------------------------------------

    def _run_tiled(self, net: _NcnnNet, img: np.ndarray, tile: int) -> np.ndarray:
        """Overlapping-tile pass, same geometry as Upscaler._tiled_inference
        (pad on every side, keep the unpadded core, average overlaps)."""
        scale = self.scale
        pad = self.tile_pad
        _, height, width = img.shape
        if tile >= max(height, width):
            return net.run(img)
        output = np.zeros((3, height * scale, width * scale), dtype=np.float32)
        weights = np.zeros((1, height * scale, width * scale), dtype=np.float32)
        for y in range(0, height, tile):
            for x in range(0, width, tile):
                y0, x0 = max(y - pad, 0), max(x - pad, 0)
                y1, x1 = min(y + tile + pad, height), min(x + tile + pad, width)
                tile_out = net.run(img[:, y0:y1, x0:x1])
                oy0, ox0 = (y - y0) * scale, (x - x0) * scale
                oy1 = oy0 + min(tile, height - y) * scale
                ox1 = ox0 + min(tile, width - x) * scale
                out_y0, out_x0 = y * scale, x * scale
                out_y1 = out_y0 + (oy1 - oy0)
                out_x1 = out_x0 + (ox1 - ox0)
                output[:, out_y0:out_y1, out_x0:out_x1] += tile_out[:, oy0:oy1, ox0:ox1]
                weights[:, out_y0:out_y1, out_x0:out_x1] += 1.0
        return output / np.maximum(weights, 1.0)

    def _attempt(self, gpu: int | None, fp16: bool, tile: int, img: np.ndarray) -> np.ndarray | None:
        """One pass; None when it raised or produced implausible output."""
        where = "cpu" if gpu is None else f"vulkan:{gpu}"
        precision = "fp16" if fp16 else "fp32"
        try:
            with _StderrCapture() as captured:
                out = self._run_tiled(self._net(gpu, fp16), img, tile)
        except Exception as exc:  # noqa: BLE001
            print(f"vulkan pass failed on {where} at tile {tile} ({precision}): {exc}")
            return None
        errors = _vulkan_error_lines(captured.text)
        if errors:
            print(
                f"vulkan pass on {where} at tile {tile} ({precision}) reported "
                f"{len(errors)} Vulkan error(s), e.g. {errors[0].strip()!r}; discarding its output"
            )
            return None
        if not _plausible(out, img, self.scale):
            print(
                f"vulkan pass on {where} at tile {tile} ({precision}) returned implausible "
                f"output (consistency {consistency_psnr(out, img, self.scale):.1f} dB, "
                f"identity {identity_psnr(out, img, self.scale):.1f} dB)"
            )
            return None
        return out

    def _infer(self, img: np.ndarray) -> tuple[np.ndarray, str, str]:
        """The ladder. Returns (output, device_kind, dtype_label)."""
        global _CPU_ONLY_REASON
        gpu = select_vulkan_device()
        if gpu is not None and _CPU_ONLY_REASON is None:
            rungs = _tile_ladder(self._base_tile)
            for i, tile in enumerate(rungs):
                self.tile = tile
                out = self._attempt(gpu, True, tile, img)
                if out is None:
                    # fp16 garbage is a precision problem, not a memory one:
                    # same tile again in fp32 before stepping down.
                    out = self._attempt(gpu, False, tile, img)
                    if out is not None:
                        return out, "gpu", "fp32"
                    if i + 1 < len(rungs):
                        print(f"vulkan: retrying at tile {rungs[i + 1]}…")
                    continue
                return out, "gpu", "fp16"
            _CPU_ONLY_REASON = (
                f"{self.model_id.value} failed every Vulkan rung "
                f"({', '.join(str(t) for t in rungs)}) on device {gpu}"
            )
            print(f"{_CPU_ONLY_REASON}; falling back to CPU for the rest of this session…")
            self._notify_cpu_fallback()
        elif gpu is not None:
            print(f"vulkan: skipping GPU rungs ({_CPU_ONLY_REASON}); running on CPU")
        # CPU rung(s): fp32 only, smallest preset tile keeps memory modest.
        cpu_tile = min(p.tile for p in NCNN_TILE_PRESETS)
        self.tile = cpu_tile
        for _ in range(_CPU_ATTEMPTS):
            out = self._attempt(None, False, cpu_tile, img)
            if out is not None:
                return out, "cpu", "fp32"
        raise RuntimeError(
            f"{self.model_id.value}: every Vulkan and CPU pass failed or "
            "returned implausible output — see the log above"
        )

    def upscale(self, image: Image.Image) -> UpscaleResult:
        # Alpha handling is identical to Upscaler.upscale: the models are
        # RGB-only, so the card's real alpha (transparent rounded corners)
        # is split off and reattached to the output, resized to match.
        alpha = image.getchannel("A") if image.mode in ("RGBA", "LA") else None
        rgb = image.convert("RGB")
        img = np.asarray(rgb, dtype=np.float32).transpose(2, 0, 1) / 255.0
        self.tile = self._base_tile
        print(f"  inference config: vulkan, tile {self.tile}")
        with self._phase("inference"):
            out, device, dtype = self._infer(img)
        out8 = (np.clip(out, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8).transpose(1, 2, 0)
        out_image = Image.fromarray(np.ascontiguousarray(out8), mode="RGB")
        if alpha is not None:
            resized_alpha = alpha.resize(out_image.size, Image.Resampling.LANCZOS)
            out_image.putalpha(resized_alpha)
        return UpscaleResult(image=out_image, device=device, dtype=dtype)


def _make_net(param: Path, weights: Path, gpu: int | None, fp16: bool) -> _NcnnNet:
    """Net construction, as a module function so tests can substitute a
    fake without touching the cache logic."""
    return _NcnnNet(param, weights, gpu, fp16)
