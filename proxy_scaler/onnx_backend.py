"""The ONNX Runtime (WebGPU) inference backend — the runtime behind
UpscaleModel.backend == Backend.ONNX: UltraSharpV2 and IllustrationJaNai on
any GPU.

Why it exists: those two are DAT transformers. ncnn can't run them (their
window attention reshapes through 5-6 D tensors; ncnn stops at 4), and the
torch path fails on some AMD stacks (ROCm on an RX 7600 XT corrupted the
display; torch-directml on an RX 9070 XT computes garbage). ONNX Runtime's
WebGPU execution provider runs the same model, exported to ONNX, through
Dawn: Vulkan on Linux, Direct3D 12 on Windows — never DirectML. Measured
86.6 dB against PyTorch fp32 on a real card tile (the torch bf16 path is
55 dB), and 58.9 dB on a whole card tiled identically. It is full
precision only (WebGPU has no bf16 and DAT is fp16-unsafe): a card takes
about 3x as long as the torch bf16 path on the same healthy GPU (3080 Ti:
~34 s at tile 192 vs ~12 s).

Shape: OnnxUpscaler mirrors upscale.Upscaler / ncnn_backend.NcnnUpscaler
(constructor kwargs, model_id / scale / tile, upscale() -> UpscaleResult),
dispatched by pipeline._upscalers_for_targets. onnxruntime is imported
lazily: this module is on the API process's import path, and the package
doesn't exist at all on macOS (the two models are hidden there).

Exports are fixed-size (upscale.ONNX_TILE_PRESETS): one .onnx per VRAM
tier, each tile edge-padded to that file's input size by host_tiling.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import host_tiling
from .upscale import (
    ONNX_TILE_PAD,
    ONNX_TILE_PRESETS,
    Backend,
    UpscaleModel,
    UpscaleResult,
    default_tile_preset,
    ensure_weight_file,
    onnx_filename,
    onnx_input_size,
    parse_model,
)

# PROXY_SCALER_ONNX_TILE: content tile in px, overrides the client's choice
# (snapped to an exported tier). Debug knob, like PROXY_SCALER_VULKAN_TILE.
ONNX_TILE_ENV = "PROXY_SCALER_ONNX_TILE"

INPUT_NAME = "data"
OUTPUT_NAME = "output"
WEBGPU_PROVIDER = "WebGpuExecutionProvider"
CPU_PROVIDER = "CPUExecutionProvider"


class OnnxUnavailable(RuntimeError):
    """onnxruntime (with the WebGPU provider) isn't in this build."""


def _ort():
    try:
        import onnxruntime
    except ImportError as exc:
        raise OnnxUnavailable(
            "ONNX Runtime isn't available in this build (there is no macOS "
            "package); pick another model"
        ) from exc
    return onnxruntime


_AVAILABLE: bool | None = None


def onnx_available() -> bool:
    """onnxruntime imports and offers the WebGPU provider. Cached; never
    raises."""
    global _AVAILABLE
    if _AVAILABLE is None:
        try:
            _AVAILABLE = WEBGPU_PROVIDER in _ort().get_available_providers()
        except Exception:  # noqa: BLE001
            _AVAILABLE = False
    return _AVAILABLE


# --- sessions -----------------------------------------------------------------


class _Session:
    """One fixed-size .onnx on one provider."""

    def __init__(self, path: Path, *, gpu: bool) -> None:
        ort = _ort()
        so = ort.SessionOptions()
        so.log_severity_level = 3
        if gpu:
            # An op ONNX Runtime can't place on the GPU must be an error,
            # never a silent partial CPU run (minutes per card, unannounced).
            so.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
            providers = [WEBGPU_PROVIDER]
        else:
            providers = [CPU_PROVIDER]
        self.gpu = gpu
        self._session = ort.InferenceSession(str(path), so, providers=providers)
        shape = self._session.get_inputs()[0].shape
        self.input_size = int(shape[-1])

    def run(self, chw: np.ndarray) -> np.ndarray:
        """float32 CHW (exactly input_size square) -> float32 CHW x4."""
        batch = np.ascontiguousarray(chw[None], dtype=np.float32)
        out = self._session.run([OUTPUT_NAME], {INPUT_NAME: batch})[0]
        return np.asarray(out[0], dtype=np.float32)


_SESSION_CACHE: dict[tuple, _Session] = {}


def clear_onnx_cache() -> None:
    """Drop the loaded session (and with it its device memory)."""
    _SESSION_CACHE.clear()


def _make_session(path: Path, gpu: bool) -> _Session:
    """Session construction, as a module function so tests can substitute
    a fake without touching the cache logic."""
    return _Session(path, gpu=gpu)


# --- tiles ----------------------------------------------------------------------

_PRESET_TILES = sorted(p.tile for p in ONNX_TILE_PRESETS)


def resolve_onnx_tile(tile_setting: int) -> int:
    """The exported tier a pass starts at. Only preset tiles have files, so
    anything else (a legacy manual number, the env override) snaps down to
    the largest tier that fits in it, or the smallest tier. 0 is the
    default tier, never "untiled"."""
    override = (os.environ.get(ONNX_TILE_ENV) or "").strip()
    wanted = tile_setting
    if override:
        try:
            wanted = int(override)
        except ValueError:
            print(f"warning: {ONNX_TILE_ENV}={override!r} ignored", file=sys.stderr)
    if wanted <= 0:
        preset = default_tile_preset(UpscaleModel.ULTRASHARP_V2_ORT)
        return preset.tile if preset else _PRESET_TILES[0]
    fitting = [t for t in _PRESET_TILES if t <= wanted]
    return fitting[-1] if fitting else _PRESET_TILES[0]


# Once a task has fallen all the way to the CPU, later tasks in this process
# skip the GPU tiers (same reasoning as ncnn_backend._CPU_ONLY_REASON).
_CPU_ONLY_REASON: str | None = None


class OnnxUpscaler:
    """ONNX Runtime twin of upscale.Upscaler for Backend.ONNX models."""

    def __init__(
        self,
        model: UpscaleModel | str,
        scale: int = 4,
        weights_dir: Path | str = "weights",
        tile: int = 0,
        tile_pad: int = ONNX_TILE_PAD,
        timings: object | None = None,
        tile_auto: bool = False,
        on_cpu_fallback: object | None = None,
    ) -> None:
        self.model_id = parse_model(model)
        if self.model_id.backend is not Backend.ONNX:
            raise TypeError(
                f"{self.model_id.value} runs on the {self.model_id.backend.value} "
                "backend; construct it via make_upscaler()"
            )
        if scale not in self.model_id.supported_scales:
            raise ValueError(
                f"{self.model_id.value} supports scales "
                f"{self.model_id.supported_scales}, not x{scale}"
            )
        self.scale = scale
        self.weights_dir = Path(weights_dir)
        self._base_tile = resolve_onnx_tile(tile)
        self.tile = self._base_tile
        # The exports bake in ONNX_TILE_PAD (input = tile + 2*pad); the
        # caller's tile_pad is accepted for signature parity only.
        self.tile_pad = ONNX_TILE_PAD
        self.tile_auto = tile_auto
        self._timings = timings
        self._on_cpu_fallback = on_cpu_fallback

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

    def _session(self, tile: int, gpu: bool) -> _Session:
        size = onnx_input_size(tile)
        key = (self.model_id, self.scale, str(self.weights_dir.resolve()), size, gpu)
        session = _SESSION_CACHE.get(key)
        if session is not None:
            return session
        with self._phase("model_load"):
            # One model slot across all three runtimes: a warm torch model
            # or ncnn net holds device memory this session needs.
            from . import upscale as _upscale
            from .ncnn_backend import clear_ncnn_cache

            _upscale.clear_model_cache()
            clear_ncnn_cache()
            _SESSION_CACHE.clear()
            path, _ = ensure_weight_file(
                self.model_id, self.scale, self.weights_dir, onnx_filename(self.model_id, size)
            )
            where = "webgpu" if gpu else "cpu"
            print(f"Loading {self.model_id.value} x{self.scale} on {where} (input {size}, {path.name})...")
            started = time.perf_counter()
            session = _make_session(path, gpu)
            print(f"  onnx session ready in {time.perf_counter() - started:.1f}s")
        _SESSION_CACHE[key] = session
        return session

    def _attempt(self, tile: int, gpu: bool, img: np.ndarray) -> np.ndarray | None:
        """One pass at one tier; None when it raised or came back
        implausible (see host_tiling.plausible)."""
        where = "webgpu" if gpu else "cpu"
        try:
            session = self._session(tile, gpu)
            with host_tiling.StderrCapture():
                out = host_tiling.run_tiled(
                    session.run,
                    img,
                    tile=tile,
                    pad=ONNX_TILE_PAD,
                    scale=self.scale,
                    fixed_size=session.input_size,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"onnx pass failed on {where} at tile {tile}: {exc}")
            return None
        if not host_tiling.plausible(out, img, self.scale):
            print(
                f"onnx pass on {where} at tile {tile} returned implausible output "
                f"(consistency {host_tiling.consistency_psnr(out, img, self.scale):.1f} dB, "
                f"identity {host_tiling.identity_psnr(out, img, self.scale):.1f} dB)"
            )
            return None
        return out

    def _infer(self, img: np.ndarray) -> tuple[np.ndarray, str]:
        """The ladder: tiers downward on the GPU, then the CPU (announced).
        Returns (output, device_kind)."""
        global _CPU_ONLY_REASON
        if _CPU_ONLY_REASON is None:
            rungs = host_tiling.tile_ladder(self._base_tile, _PRESET_TILES)
            for i, tile in enumerate(rungs):
                self.tile = tile
                out = self._attempt(tile, True, img)
                if out is not None:
                    return out, "gpu"
                if i + 1 < len(rungs):
                    print(f"onnx: retrying at tile {rungs[i + 1]}…")
            _CPU_ONLY_REASON = (
                f"{self.model_id.value} failed every GPU tier "
                f"({', '.join(str(t) for t in rungs)})"
            )
            print(f"{_CPU_ONLY_REASON}; falling back to CPU for the rest of this session…")
            self._notify_cpu_fallback()
        else:
            print(f"onnx: skipping GPU tiers ({_CPU_ONLY_REASON}); running on CPU")
        self.tile = _PRESET_TILES[0]
        out = self._attempt(self.tile, False, img)
        if out is None:
            raise RuntimeError(
                f"{self.model_id.value}: every GPU and CPU pass failed or returned "
                "implausible output — see the log above"
            )
        return out, "cpu"

    def upscale(self, image: Image.Image) -> UpscaleResult:
        img, alpha = host_tiling.split_image(image)
        self.tile = self._base_tile
        print(
            f"  inference config: webgpu fp32, tile {self.tile} "
            f"(input {onnx_input_size(self.tile)})"
        )
        with self._phase("inference"):
            out, device = self._infer(img)
        return UpscaleResult(image=host_tiling.join_image(out, alpha), device=device, dtype="fp32")
