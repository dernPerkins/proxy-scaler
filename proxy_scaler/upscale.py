"""Multi-model upscaling (Anime Fast, UltraSharpV2, IllustrationJaNai) via Spandrel."""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from PIL import Image

# torch/spandrel/torchvision are deliberately NOT imported at module scope.
# This module is on the FastAPI server's import path (via pipeline.py, used
# by nearly every router) — an eager import here means the whole API
# process, including trivial DB-backed endpoints like listing projects,
# can't even bind its port until the entire ML stack finishes loading.
# Real-world impact: the desktop app's "local server ready" check polls
# /api/health, so this alone determined how long the UI sat blocked before
# a user could do anything at all, generation-related or not. Every
# function below that actually needs these imports them locally instead.
if TYPE_CHECKING:
    import torch
    from spandrel import ImageModelDescriptor


class UpscaleModel(str, Enum):
    REALESRGAN_ANIME_FAST = "realesrgan_anime_fast"
    ILLUSTRATIONJANAI = "illustrationjanai"
    ULTRASHARP_V2 = "ultrasharp_v2"

    @property
    def label(self) -> str:
        return {
            UpscaleModel.REALESRGAN_ANIME_FAST: (
                "Real-ESRGAN Anime Fast (compact/lightweight, tuned for anime video)"
            ),
            UpscaleModel.ILLUSTRATIONJANAI: (
                "IllustrationJaNai (trained on digital art/illustrations, not photos)"
            ),
            UpscaleModel.ULTRASHARP_V2: (
                "UltraSharpV2 (general-purpose, strong on illustration/artwork)"
            ),
        }[self]

    @property
    def supported_scales(self) -> tuple[int, ...]:
        # Every current model is x4-only.
        return (4,)


# Transformer/attention-heavy architectures that can OOM a ~12GB GPU on a
# full-image forward pass — the lighter CNN-based models don't need tiling.
# Ported from the old Streamlit UI (ui/decklist.py::_effective_tile_size)
# when that module was deleted — real behavior, not UI-specific, so it
# belongs here next to UpscaleModel rather than disappearing with the UI.
HEAVY_MODELS = frozenset(
    {
        UpscaleModel.ILLUSTRATIONJANAI,
        UpscaleModel.ULTRASHARP_V2,
    }
)
DEFAULT_TILE_SIZE = 384


def effective_tile_size(model: UpscaleModel, tile_size_setting: int) -> int:
    """0 (not manually set) auto-falls-back to DEFAULT_TILE_SIZE for heavy
    models only, leaving already-working lighter models untouched. An
    explicit non-zero setting always wins, regardless of model."""
    if tile_size_setting > 0:
        return tile_size_setting
    return DEFAULT_TILE_SIZE if model in HEAVY_MODELS else 0


@dataclass(frozen=True)
class _WeightSpec:
    filename: str
    url: str


# Official release weights loadable by Spandrel
_WEIGHTS: dict[tuple[UpscaleModel, int], _WeightSpec] = {
    # Official release — the "Compact" (SRVGGNetCompact) architecture,
    # much smaller/faster than the RRDBNet-based anime models.
    (UpscaleModel.REALESRGAN_ANIME_FAST, 4): _WeightSpec(
        "realesr-animevideov3.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth",
    ),
    # CC-BY-NC-SA-4.0 (non-commercial). Original author hosts on Google
    # Drive only, which our simple streaming downloader can't handle for
    # files this size — using a third-party HuggingFace mirror instead.
    # If this mirror ever disappears, search for
    # "4x_IllustrationJaNai_V1_DAT2" on huggingface.co for a replacement.
    (UpscaleModel.ILLUSTRATIONJANAI, 4): _WeightSpec(
        "4x_IllustrationJaNai_V1_DAT2_190k.pth",
        "https://huggingface.co/tomjackson2023/upscale_models/resolve/main/4x_IllustrationJaNai_V1_DAT2_190k.pth",
    ),
    # CC-BY-NC-SA-4.0 (non-commercial). Officially hosted by the creator.
    (UpscaleModel.ULTRASHARP_V2, 4): _WeightSpec(
        "4x-UltraSharpV2.safetensors",
        "https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2.safetensors",
    ),
}


def parse_model(value: str | UpscaleModel) -> UpscaleModel:
    if isinstance(value, UpscaleModel):
        return value
    try:
        return UpscaleModel(value.lower().strip())
    except ValueError as exc:
        choices = ", ".join(m.value for m in UpscaleModel)
        raise ValueError(f"Unknown model {value!r}. Choose one of: {choices}") from exc


def resolve_device() -> torch.device:
    import torch

    if torch.cuda.is_available():
        # Also where a ROCm-built torch (AMD on Linux) lands: ROCm's HIP
        # backend deliberately mirrors the cuda namespace end to end
        # (is_available(), device("cuda"), OutOfMemoryError, empty_cache(),
        # synchronize()), so no separate branch is needed for it here or
        # anywhere else in this module.
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    # AMD on Windows: no ROCm build exists for Windows, so torch-directml
    # (Microsoft's DirectX12-backed torch device, works with any
    # DirectX12 GPU — AMD/Intel/Nvidia) is the realistic path. Its device
    # lives on torch's "privateuseone" backend, invisible to the checks
    # above — only installed in a directml-flavored build (see Makefile's
    # GPU_VARIANT), so this import is optional everywhere else.
    try:
        import torch_directml
    except ImportError:
        pass
    else:
        if torch_directml.is_available():
            return torch_directml.device()
    return torch.device("cpu")


def _is_oom_error(exc: BaseException) -> bool:
    import torch

    if isinstance(exc, torch.cuda.OutOfMemoryError):
        return True
    # MPS / older torch sometimes raise RuntimeError with this message
    msg = str(exc).lower()
    return "out of memory" in msg or "oom" in msg


def _clear_device_cache(device: torch.device | None) -> None:
    if device is None:
        return
    import torch

    if device.type == "cuda" and torch.cuda.is_available():
        torch.cuda.empty_cache()
    elif device.type == "mps" and hasattr(torch, "mps"):
        try:
            torch.mps.empty_cache()
        except Exception:  # noqa: BLE001
            pass
    # device.type == "privateuseone" (DirectML) intentionally falls
    # through and does nothing — torch-directml's public API has no
    # empty_cache()-equivalent to call.


def device_kind(device: torch.device | str | None) -> str:
    """Normalize torch device to 'gpu' | 'cpu' for gallery provenance."""
    if device is None:
        return "unknown"
    import torch

    name = device.type if isinstance(device, torch.device) else str(device).lower()
    if name == "cpu":
        return "cpu"
    # "privateuseone" is torch-directml's (AMD-on-Windows) backend name;
    # "directml" is a defensive alias in case that ever changes upstream.
    if name in ("cuda", "mps", "gpu", "privateuseone", "directml"):
        return "gpu"
    return name or "unknown"


def device_backend(device: torch.device | str | None) -> str:
    """The *actual* torch backend name — "cuda" | "mps" | "privateuseone" |
    "cpu" | "unknown" — as opposed to device_kind()'s deliberately coarse
    gpu/cpu answer.

    Two distinct consumers, hence two functions rather than one:

    - device_kind() is gallery/cache provenance. Its values are persisted
      into on-disk `.device` sidecar files (see write_cache_device), so
      its vocabulary can't change without invalidating existing caches.
    - this one is a live capability signal for the client, which needs to
      tell Apple's MPS apart from CUDA to pick a sensible default model
      (MPS is a real GPU but far slower on the heavy transformer models —
      see ProjectContext.tsx::recommendedDefaultModel()). Never written
      to disk; safe to extend as new backends appear.

    Not normalized beyond lowercasing, on purpose: "privateuseone" is
    torch-directml's own backend name and callers match on it directly.
    """
    if device is None:
        return "unknown"
    import torch

    name = device.type if isinstance(device, torch.device) else str(device).lower()
    # torch.device("cuda:0").type is already "cuda", but a bare string like
    # "cuda:0" isn't — strip any index so both forms agree.
    name = name.split(":", 1)[0]
    return name or "unknown"


def _bf16_supported(device: torch.device | None) -> bool:
    """Whether this device can run bf16 inference at a real speedup.

    cuda: is_bf16_supported() — Ampere+/RDNA2+ (ROCm's HIP backend mirrors
    the cuda namespace, so AMD-on-Linux answers through the same call).
    mps: probed with a tiny op — torch's MPS bf16 support depends on the
    chip (M1 lacks it) and torch version, so asking beats version-matrixing.
    cpu / privateuseone (DirectML): no — bf16 there is emulated or absent.
    """
    if device is None:
        return False
    import torch

    if device.type == "cuda":
        try:
            return bool(torch.cuda.is_bf16_supported())
        except Exception:  # noqa: BLE001
            return False
    if device.type == "mps":
        try:
            x = torch.zeros(2, device=device, dtype=torch.bfloat16)
            (x + 1).sum().item()
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def resolve_dtype(descriptor: ImageModelDescriptor, device: torch.device) -> torch.dtype:
    """bf16 when both the model and the device support it, else fp32.

    Model-agnostic on purpose: the gate is spandrel's per-descriptor
    supports_bfloat16 flag, never the model's identity, so any model added
    to _WEIGHTS inherits the fast path automatically. fp16 is deliberately
    not attempted — spandrel blocks it for the DAT models (numerically
    unsafe), and benchmarks showed fp16 autocast gains nothing anyway (the
    DAT models are memory-bound; bf16's halved activation traffic is the
    lever, measured ~1.65x at 58dB PSNR vs fp32).
    """
    import torch

    if getattr(descriptor, "supports_bfloat16", False) and _bf16_supported(device):
        return torch.bfloat16
    return torch.float32


def _dtype_label(dtype: "torch.dtype | None") -> str:
    import torch

    return "bf16" if dtype == torch.bfloat16 else "fp32"


# A tile-768 bf16 pass on a card-sized image peaks at ~6.3 GiB allocated
# (weights included, measured on a 3080 Ti); requiring 8 GiB free keeps
# real headroom and correctly excludes 8 GB cards.
_TILE768_MIN_FREE = 8 * 1024**3
_AUTO_TILE_UPGRADE = 768


def _choose_auto_tile(
    free_bytes: int | None, dtype: "torch.dtype", base_tile: int
) -> int:
    """Pick the auto tile size given free VRAM. Only ever upgrades an
    auto-tiled heavy model (base_tile > 0) running bf16 — fp32 at 768
    OOMs even on 12 GB cards, and untiled light models stay untiled."""
    import torch

    if (
        base_tile > 0
        and dtype == torch.bfloat16
        and free_bytes is not None
        and free_bytes >= _TILE768_MIN_FREE
    ):
        return _AUTO_TILE_UPGRADE
    return base_tile


@dataclass(frozen=True)
class UpscaleResult:
    """Upscaled image plus where/how inference ran."""

    image: Image.Image
    device: str  # "gpu" | "cpu"
    dtype: str = "fp32"  # "bf16" | "fp32"


def ensure_weights(
    model: UpscaleModel,
    scale: int,
    weights_dir: Path,
) -> Path:
    if scale not in model.supported_scales:
        raise ValueError(
            f"{model.value} supports scales {model.supported_scales}, not x{scale}"
        )
    spec = _WEIGHTS[(model, scale)]
    weights_dir.mkdir(parents=True, exist_ok=True)
    path = weights_dir / spec.filename
    if path.exists() and path.stat().st_size > 0:
        return path

    print(f"Downloading {spec.filename} …")
    resp = requests.get(spec.url, timeout=120, stream=True)
    resp.raise_for_status()
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("wb") as fp:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fp.write(chunk)
    tmp.replace(path)
    print(f"Saved weights to {path}")
    return path


# One-slot cache of the loaded (device-resident, dtype-converted) model
# descriptor, shared across the per-task Upscaler instances. Decks are
# homogeneous, so one slot gets a ~99% hit rate and eliminates the
# per-card disk read + PCIe transfer (~0.6s/task measured). Only the
# worker's main thread loads/runs models in-process (the finisher and
# prefetch threads never touch them), so no locking; the API process has
# its own interpreter and thus its own cache.
_MODEL_CACHE: dict[tuple, "ImageModelDescriptor"] = {}


def _cache_key(model_id: UpscaleModel, scale: int, weights_dir: Path) -> tuple:
    return (model_id, scale, str(Path(weights_dir).resolve()))


def clear_model_cache() -> None:
    """Drop the cached descriptor and release its VRAM. The reference must
    go before empty_cache() — the allocator only frees unreferenced blocks."""
    devices = []
    for descriptor in _MODEL_CACHE.values():
        try:
            devices.append(next(descriptor.model.parameters()).device)
        except Exception:  # noqa: BLE001 — best-effort release
            pass
    _MODEL_CACHE.clear()
    for device in devices:
        _clear_device_cache(device)


def _cache_put(key: tuple, descriptor: "ImageModelDescriptor") -> None:
    if key not in _MODEL_CACHE:
        # One slot: switching models evicts the previous descriptor and
        # frees its VRAM before the new one settles in.
        clear_model_cache()
    _MODEL_CACHE[key] = descriptor


class Upscaler:
    """Lazy-loaded Spandrel upscaler for a chosen model + scale."""

    def __init__(
        self,
        model: UpscaleModel | str = UpscaleModel.ULTRASHARP_V2,
        scale: int = 4,
        weights_dir: Path | str = "weights",
        tile: int = 0,
        tile_pad: int = 32,
        timings: object | None = None,
        tile_auto: bool = False,
    ) -> None:
        self.model_id = parse_model(model)
        if scale not in self.model_id.supported_scales:
            raise ValueError(
                f"{self.model_id.value} supports scales "
                f"{self.model_id.supported_scales}, not x{scale}"
            )
        self.scale = scale
        self.weights_dir = Path(weights_dir)
        self.tile = tile
        self.tile_pad = tile_pad
        # tile_auto marks `tile` as the auto default rather than a user
        # choice — only then may _load_model upgrade it from free VRAM.
        self.tile_auto = tile_auto
        # Duck-typed timing_db.TimingCollector (kept untyped so this module
        # never imports timing_db, which imports db, which imports us).
        self._timings = timings
        self._descriptor: ImageModelDescriptor | None = None
        self._device: torch.device | None = None
        self._dtype: torch.dtype | None = None
        self._tile_upgraded = False

    def _phase(self, name: str):
        if self._timings is not None:
            return self._timings.phase(name)
        return contextlib.nullcontext()

    def _ensure_model(self) -> ImageModelDescriptor:
        if self._descriptor is not None:
            return self._descriptor
        key = _cache_key(self.model_id, self.scale, self.weights_dir)
        descriptor = _MODEL_CACHE.get(key)
        if descriptor is None:
            # The model_load timing phase wraps only a real load — a cache
            # hit records no phase, so NULL model_load_s in the timing DB
            # is the hit signal (same convention as download_s).
            with self._phase("model_load"):
                descriptor = self._load_model()
            _cache_put(key, descriptor)
        import torch

        # Per-instance state is derived from the descriptor's own weights,
        # hit or miss: a shared descriptor that _relocate_to_cpu() moved to
        # CPU/fp32 in a previous task is then described truthfully instead
        # of through stale cached metadata.
        param = next(descriptor.model.parameters())
        self._device = param.device
        self._dtype = (
            torch.bfloat16 if param.dtype == torch.bfloat16 else torch.float32
        )
        self._descriptor = descriptor
        self._maybe_upgrade_tile()
        # The one line a user report needs: which capability gates fired.
        print(f"  inference config: {_dtype_label(self._dtype)}, tile {self.tile or 'off'}")
        return descriptor

    def _maybe_upgrade_tile(self) -> None:
        """Per task (mem_get_info is cheap): a heavy model's auto tile may
        grow to 768 when running bf16 with enough free VRAM."""
        import torch

        if self._device is None or self._device.type != "cuda":
            return
        if not (self.tile_auto and self.tile > 0):
            return
        try:
            free_bytes, _ = torch.cuda.mem_get_info()
            # mem_get_info counts this process's allocator arenas as "used",
            # but reserved-yet-unallocated blocks are reusable for our own
            # tiles — without adding them back, the first task's warm
            # allocator (no more per-image empty_cache) would push every
            # later task below the gate and silently downgrade to 384.
            free_bytes += torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
        except Exception:  # noqa: BLE001
            free_bytes = None
        new_tile = _choose_auto_tile(free_bytes, self._dtype, self.tile)
        if new_tile != self.tile:
            self.tile = new_tile
            self._tile_upgraded = True

    def _load_model(self) -> ImageModelDescriptor:
        import torch
        from spandrel import ImageModelDescriptor, ModelLoader

        device = resolve_device()
        weights = ensure_weights(self.model_id, self.scale, self.weights_dir)
        print(f"Loading {self.model_id.value} x{self.scale} on {device} ({weights.name})...")
        if device.type != "cpu":
            print(
                "note: PyTorch may print its own "
                "'[W...] memory allocation failed with OOM' lines below while "
                "upscaling — those come from CUDA's allocator retrying "
                "internally and usually resolve on their own. This app only "
                "reports a real failure with its own 'Upscale OOM on ...; "
                "clearing cache and retrying on CPU…' message."
            )
        descriptor = ModelLoader().load_from_file(str(weights))
        if not isinstance(descriptor, ImageModelDescriptor):
            raise TypeError(f"Unexpected model type for {weights}")
        dtype = resolve_dtype(descriptor, device)
        try:
            descriptor = descriptor.to(device).eval()
            if dtype == torch.bfloat16:
                try:
                    descriptor = descriptor.to(torch.bfloat16)
                except Exception:  # noqa: BLE001 — any bf16 hiccup means fp32
                    pass  # weights stay fp32; _ensure_model derives that
        except Exception as exc:
            if device.type != "cpu" and _is_oom_error(exc):
                print(
                    f"OOM loading model on {device}; clearing cache and falling back to CPU…"
                )
                _clear_device_cache(device)
                descriptor = descriptor.to(torch.device("cpu")).eval()
            else:
                raise
        return descriptor

    def _relocate_to_cpu(self) -> ImageModelDescriptor:
        """Move loaded weights to CPU after a GPU OOM (stays on CPU afterward)."""
        import torch

        assert self._descriptor is not None
        old = self._device
        _clear_device_cache(old)
        print(f"Falling back to CPU upscale (was {old})…")
        self._device = torch.device("cpu")
        # bf16 on CPU is emulated/slow — fall all the way back to fp32.
        self._dtype = torch.float32
        self._descriptor = self._descriptor.to(self._device).to(torch.float32).eval()
        return self._descriptor

    def _run_inference(
        self,
        descriptor: ImageModelDescriptor,
        tensor: torch.Tensor,
    ) -> torch.Tensor:
        if self.tile and min(tensor.shape[-2:]) > self.tile:
            return self._tiled_inference(descriptor, tensor)
        return descriptor(tensor)

    def upscale(self, image: Image.Image) -> UpscaleResult:
        # Equivalent to decorating with @torch.inference_mode() — kept as an
        # explicit context manager so torch doesn't need to be importable at
        # class-definition time (a decorator argument evaluates when this
        # method is *defined*, i.e. at module import, which is exactly the
        # eager-import cost this module is otherwise built to avoid).
        import torch
        from torchvision.transforms.functional import to_pil_image, to_tensor

        with torch.inference_mode():
            descriptor = self._ensure_model()
            assert self._device is not None
            # Scryfall PNGs carry real per-card alpha (transparent rounded
            # corners). The models are RGB-only, so the alpha channel is
            # split off here and reattached to the model's output below,
            # resized to match — preserving the card's actual corner shape
            # instead of letting it get silently discarded.
            alpha = image.getchannel("A") if image.mode in ("RGBA", "LA") else None
            rgb = image.convert("RGB")
            dtype = self._dtype or torch.float32
            tensor = to_tensor(rgb).unsqueeze(0).to(self._device, dtype)

            try:
                # The inference phase deliberately spans the OOM retries,
                # so inference_s is the true wall-clock cost including any
                # fallback (tile downgrade or model relocation + re-run).
                with self._phase("inference"):
                    try:
                        out_gpu = self._run_inference(descriptor, tensor)
                    except Exception as exc:
                        if self._device.type == "cpu" or not _is_oom_error(exc):
                            raise
                        if self._tile_upgraded:
                            # The VRAM-probed larger tile turned out not to
                            # fit after all (something else grabbed VRAM) —
                            # drop back to the base tile on the SAME device
                            # before resorting to the catastrophic CPU path.
                            base = effective_tile_size(self.model_id, 0)
                            print(
                                f"Upscale OOM on {self._device} at tile "
                                f"{self.tile}; retrying at tile {base}…"
                            )
                            self.tile = base
                            self._tile_upgraded = False
                            _clear_device_cache(self._device)
                            try:
                                out_gpu = self._run_inference(descriptor, tensor)
                            except Exception as exc2:
                                if not _is_oom_error(exc2):
                                    raise
                                exc = exc2
                            else:
                                exc = None
                        if exc is not None:
                            print(
                                f"Upscale OOM on {self._device}; clearing cache and retrying on CPU…"
                            )
                            _clear_device_cache(self._device)
                            if self._device.type == "cuda":
                                try:
                                    torch.cuda.synchronize(self._device)
                                except Exception:  # noqa: BLE001
                                    pass
                            del tensor
                            descriptor = self._relocate_to_cpu()
                            tensor = to_tensor(rgb).unsqueeze(0).to(
                                self._device, self._dtype or torch.float32
                            )
                            out_gpu = self._run_inference(descriptor, tensor)

                out_cpu = out_gpu.clamp(0.0, 1.0).squeeze(0).float().cpu()
                del out_gpu
                out_image = to_pil_image(out_cpu)
                if alpha is not None:
                    resized_alpha = alpha.resize(out_image.size, Image.Resampling.LANCZOS)
                    out_image.putalpha(resized_alpha)
                return UpscaleResult(
                    image=out_image,
                    device=device_kind(self._device),
                    dtype=_dtype_label(self._dtype),
                )
            finally:
                # Deliberately NO empty_cache() here: clearing per image
                # forced the CUDA allocator to re-grow its arenas on every
                # task. The allocator reuses the freed blocks for the next
                # card; explicit clears remain only on the OOM paths and on
                # model-cache eviction.
                try:
                    del tensor
                except NameError:
                    pass

    def _tiled_inference(
        self,
        descriptor: ImageModelDescriptor,
        img: torch.Tensor,
    ) -> torch.Tensor:
        """Simple overlapping-tile inference for large images / low VRAM."""
        assert self._device is not None
        scale = self.scale
        tile = self.tile
        pad = self.tile_pad
        _, _, height, width = img.shape
        output = img.new_zeros((1, 3, height * scale, width * scale))
        weights = img.new_zeros((1, 1, height * scale, width * scale))

        for y in range(0, height, tile):
            for x in range(0, width, tile):
                y0, x0 = max(y - pad, 0), max(x - pad, 0)
                y1, x1 = min(y + tile + pad, height), min(x + tile + pad, width)
                tile_in = img[:, :, y0:y1, x0:x1]
                tile_out = descriptor(tile_in)

                # Valid (unpadded) region in output space
                oy0, ox0 = (y - y0) * scale, (x - x0) * scale
                oy1 = oy0 + min(tile, height - y) * scale
                ox1 = ox0 + min(tile, width - x) * scale
                out_y0, out_x0 = y * scale, x * scale
                out_y1 = out_y0 + (oy1 - oy0)
                out_x1 = out_x0 + (ox1 - ox0)

                patch = tile_out[:, :, oy0:oy1, ox0:ox1]
                output[:, :, out_y0:out_y1, out_x0:out_x1] += patch
                weights[:, :, out_y0:out_y1, out_x0:out_x1] += 1.0

        return output / weights.clamp_min(1.0)


def _tmp_sibling(path: Path) -> Path:
    """Unique-per-writer temp name next to `path`. pid+tid keeps two
    concurrent writers (worker main thread, prefetch thread, finisher
    thread — or a killed process's leftovers) from ever sharing a tmp
    file, and the suffix never matches the exists()/glob checks that look
    for exact final names, so a crash leaves only inert debris."""
    import os
    import threading

    return path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write-then-rename so `path` only ever holds complete content."""
    import os

    tmp = _tmp_sibling(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_save_png(image: Image.Image, path: Path, *, compress_level: int = 6) -> None:
    """PNG save via tmp+rename; compress_level 6 is PIL's own default."""
    import os

    tmp = _tmp_sibling(path)
    try:
        image.save(tmp, format="PNG", compress_level=compress_level)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_cache_png(image: Image.Image, path: Path, device: str, *, compress_level: int = 3) -> None:
    """Atomic upscale-cache write. The device sidecar is written strictly
    AFTER the PNG lands — a sidecar must never describe a missing or
    partial PNG. compress_level 3: this is an internal cache file the
    worker only ever writes (tasks run force=True), so encode speed beats
    the ~10-15% size cost."""
    atomic_save_png(image, path, compress_level=compress_level)
    write_cache_device(path, device)


def cache_path(
    cache_dir: Path,
    scryfall_id: str,
    face_index: int | None,
    scale: int,
    model: UpscaleModel | str,
) -> Path:
    model_id = parse_model(model)
    face_part = "single" if face_index is None else f"face{face_index}"
    return cache_dir / f"{scryfall_id}_{face_part}_{model_id.value}_x{scale}.png"


def original_cache_path(
    cache_dir: Path,
    scryfall_id: str,
    face_index: int | None,
) -> Path:
    face_part = "single" if face_index is None else f"face{face_index}"
    return cache_dir / "originals" / f"{scryfall_id}_{face_part}.png"


def original_thumb_path(original_path: Path) -> Path:
    """Sibling small-JPEG preview thumbnail for a cached original PNG —
    originals/<id>_<face>.png -> originals/<id>_<face>_thumb.jpg. Derived,
    not stored: same convention as original_cache_path/cache_path
    themselves (see pipeline.py::ensure_original_thumbnail for the
    generate-on-demand side of this)."""
    return original_path.with_name(original_path.stem + "_thumb.jpg")


def cache_device_path(cache_png: Path) -> Path:
    """Sidecar file recording whether a cached upscale ran on gpu or cpu."""
    return Path(str(cache_png) + ".device")


def read_cache_device(cache_png: Path) -> str:
    meta = cache_device_path(cache_png)
    if not meta.is_file():
        return "unknown"
    try:
        value = meta.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "unknown"
    return value if value in ("gpu", "cpu") else "unknown"


def write_cache_device(cache_png: Path, device: str) -> None:
    kind = device_kind(device)
    if kind not in ("gpu", "cpu"):
        return
    try:
        cache_device_path(cache_png).write_text(kind + "\n", encoding="utf-8")
    except OSError:
        pass


def load_or_upscale(
    *,
    png_bytes: bytes,
    upscaler: Upscaler,
    cache_dir: Path,
    scryfall_id: str,
    face_index: int | None,
    force: bool = False,
    timings: object | None = None,
    defer_cache_write: bool = False,
) -> UpscaleResult:
    """Return upscaled image (+ device), using disk cache when present.

    `timings` is a duck-typed timing_db.TimingCollector (or None); a cache
    hit records nothing.

    `defer_cache_write=True` skips the cache PNG + sidecar write entirely —
    the caller owns it (the worker's deferred-finish tail reconstructs the
    path via cache_path() and writes via save_cache_png() off the GPU's
    critical path). On a cache hit there is nothing to write, so the flag
    is a no-op there."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(
        cache_dir,
        scryfall_id,
        face_index,
        upscaler.scale,
        upscaler.model_id,
    )
    if path.exists() and not force:
        cached = Image.open(path)
        cached_image = (
            cached.convert("RGBA")
            if cached.mode in ("RGBA", "LA")
            else cached.convert("RGB")
        )
        return UpscaleResult(
            image=cached_image,
            device=read_cache_device(path),
        )

    src = Image.open(io.BytesIO(png_bytes))
    if timings is not None:
        timings.set_src_dims(*src.size)
    result = upscaler.upscale(src)
    if defer_cache_write:
        return result
    with timings.phase("encode") if timings is not None else contextlib.nullcontext():
        save_cache_png(result.image, path, result.device)
    return result
