"""Multi-model upscaling via Spandrel (see UpscaleModel for the roster)."""

from __future__ import annotations

import contextlib
import io
import os
import sys
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
    # NOTE for future members: db.py's filename-slug regex sorts enum
    # values longest-first, which is the only thing making a value that
    # prefixes another (like this one vs ultrasharp_v2) safe.
    ULTRASHARP_V2_LITE = "ultrasharp_v2_lite"

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
            UpscaleModel.ULTRASHARP_V2_LITE: (
                "UltraSharpV2 Lite (general-purpose, faster sibling of UltraSharpV2)"
            ),
        }[self]

    @property
    def speed(self) -> str:
        """Relative-speed wording for model menus — the honest trade-off a
        user is making, in their terms. Same all-members-dict style as
        label so a forgotten member fails loudly, not silently."""
        return {
            UpscaleModel.REALESRGAN_ANIME_FAST: "Fastest",
            UpscaleModel.ILLUSTRATIONJANAI: "Best for illustrations — slowest",
            UpscaleModel.ULTRASHARP_V2: "Best quality — slowest",
            UpscaleModel.ULTRASHARP_V2_LITE: "Balanced",
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
    # The author's own companion release to UltraSharpV2, from the same
    # repo under the same CC-BY-NC-SA-4.0 license. RealPLKSR architecture:
    # a small CNN (~30MB weights vs 140MB) that sits between the compact
    # video model and the DAT2 transformers on both speed and quality —
    # light enough to run untiled (not in HEAVY_MODELS).
    (UpscaleModel.ULTRASHARP_V2_LITE, 4): _WeightSpec(
        "4x-UltraSharpV2_Lite.safetensors",
        "https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2_Lite.safetensors",
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


# ---- Auto tile ladder ------------------------------------------------
#
# Measured on a 3080 Ti (12 GB, torch 2.14 cu130), bf16, warm model, on a
# 745x1040 Scryfall card. "reserved" is what the CUDA caching allocator
# actually holds — the number a GPU runs out of — first with torch's
# default allocator and then with expandable_segments (worker.py sets it):
#
#   tile      secs   allocated   reserved(default)   reserved(expandable)
#   untiled    7.7    6.27 GiB     10.76 GiB            6.54 GiB
#   640        8.4    3.66          7.26                3.77
#   512        8.6    2.59          5.57                2.75
#   384        9.1    1.63          3.38                1.71
#   256        9.8    0.98          1.89                1.03
#
# Three things fall out of that table:
#  - Bigger tiles buy little in bf16 (~16% untiled vs 384), so an
#    aggressive ladder is a memory risk for a small speed reward.
#  - The default allocator reserves ~2x what it allocates on the DAT
#    models — the untiled pass reserved 10.8 GiB on a 12 GB card. On
#    Windows the Nvidia driver's default sysmem-fallback policy then
#    spills that growth into system RAM rather than failing, so torch
#    never raises the OOM the retry ladder below depends on and the GPU
#    thrashes instead. expandable_segments removes the overhead (reserved
#    lands within ~4% of allocated), which is why the worker enables it.
#  - Need is linear in the padded tile's pixel count plus a small fixed
#    base (weights + workspace); the fit below reproduces every measured
#    allocated peak within ~0.1 GiB, so a rung's need can be estimated
#    for any image size instead of hard-coding thresholds per rung.
#
# The ladder is largest first; 0 means untiled (the same value
# _run_inference treats as "no tiling"). It also drives the OOM retry
# path: a rung that OOMs steps down to whatever the re-probed free VRAM
# says fits. 256 is the floor — the last GPU rung before the terminal CPU
# relocation, never gated (below its ~1 GiB the DAT models are hopeless on
# that GPU anyway).
_AUTO_TILE_LADDER = (0, 640, 512, 384, 256)
_AUTO_TILE_FLOOR = 256
# Light models run untiled (effective_tile_size gives 0) and never exceed
# ~0.8 GiB on a card, so the ladder has nothing to offer them except a
# single tiled retry before the catastrophic CPU fallback on tiny GPUs.
_LIGHT_MODEL_RETRY_TILE = 384
# Cost model (bytes): base + pixels_in_largest_padded_tile * per_px. Fit to
# the bf16 column above; fp32 doubles the activation term (its measured
# 3.24 GiB at tile 384 sits within 0.15 GiB of the estimate).
_VRAM_BASE_BYTES = 180 * 1024**2
_VRAM_BYTES_PER_PX = {"bf16": 8450, "fp32": 16900}
# Headroom over the estimate. 1.4x once the allocator is known to behave
# (reserved ≈ allocated); the first task of a process runs at 2x in case
# expandable_segments was ignored on this platform — torch then reserves
# ~2x — and the observed reserved/allocated ratio replaces the guess
# afterwards (see Upscaler._record_allocator_headroom).
_VRAM_HEADROOM_DEFAULT = 1.4
_VRAM_HEADROOM_FIRST_TASK = 2.0
# The untiled rung's explicit floor, on top of the formula (which lands at
# ~8.8 GiB for a card at 1.4x): free VRAM only, deliberately not total —
# what the card can do right now is what matters.
_UNTILED_MIN_FREE = 9 * 1024**3


def _rung_order(tile: int) -> float:
    """Sort key for ladder rungs: 0 (untiled) is the largest."""
    return float("inf") if tile <= 0 else float(tile)


def _ladder_step_down(tile: int) -> int | None:
    """The next smaller ladder rung below `tile`, or None at the floor."""
    for rung in _AUTO_TILE_LADDER:
        if _rung_order(rung) < _rung_order(tile):
            return rung
    return None


def _max_padded_tile_px(width: int, height: int, tile: int, pad: int) -> int:
    """Pixel count of the largest tile _tiled_inference would feed the
    model for this image — the quantity VRAM need scales with. Mirrors
    _run_inference's skip condition (an image no bigger than the tile runs
    untiled) and _tiled_inference's padded bounds exactly."""
    if tile <= 0 or min(width, height) <= tile:
        return width * height
    largest = 0
    for y in range(0, height, tile):
        y0, y1 = max(y - pad, 0), min(y + tile + pad, height)
        for x in range(0, width, tile):
            x0, x1 = max(x - pad, 0), min(x + tile + pad, width)
            largest = max(largest, (y1 - y0) * (x1 - x0))
    return largest


def _estimated_vram_need(
    width: int,
    height: int,
    tile: int,
    pad: int,
    dtype_label: str,
    headroom: float,
) -> int:
    """Free VRAM a rung wants before the ladder will pick it (bytes)."""
    px = _max_padded_tile_px(width, height, tile, pad)
    per_px = _VRAM_BYTES_PER_PX.get(dtype_label, _VRAM_BYTES_PER_PX["fp32"])
    need = int((_VRAM_BASE_BYTES + px * per_px) * headroom)
    if px == width * height:
        # Resolves to a full-image pass for this image: the explicit floor
        # applies whichever rung got it there.
        need = max(need, _UNTILED_MIN_FREE)
    return need


def _choose_auto_tile(
    free_bytes: int | None,
    dtype_label: str,
    base_tile: int,
    *,
    width: int,
    height: int,
    pad: int,
    headroom: float = _VRAM_HEADROOM_DEFAULT,
    below: int | None = None,
) -> int:
    """Pick the auto tile for a heavy model (base_tile > 0): the largest
    ladder rung whose estimated need fits `free_bytes`, else the floor.
    The base is just another rung — nothing is special about 384 beyond
    being the answer when free VRAM is unknown (benefit of the doubt).
    `below` (the OOM retry path) restricts the walk to rungs strictly
    smaller than the one that just failed. Untiled light models (base 0)
    and manual tile settings are never touched (callers gate on
    tile_auto)."""
    if base_tile <= 0:
        return base_tile
    if free_bytes is None:
        return base_tile
    for rung in _AUTO_TILE_LADDER:
        if below is not None and _rung_order(rung) >= _rung_order(below):
            continue
        need = _estimated_vram_need(width, height, rung, pad, dtype_label, headroom)
        if free_bytes >= need:
            return rung
    return _AUTO_TILE_FLOOR


@dataclass(frozen=True)
class UpscaleResult:
    """Upscaled image plus where/how inference ran."""

    image: Image.Image
    device: str  # "gpu" | "cpu"
    dtype: str = "fp32"  # "bf16" | "fp32"
    # True when the image came from the x4 cache PNG instead of a fresh
    # model pass — the caller then knows there is nothing to write back.
    from_cache: bool = False


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


# ---- DirectML: channel-wise PReLU workaround ------------------------
#
# torch-directml 0.2.5 (torch 2.4.1) aborts the whole process — a native
# CHECK, not a Python exception — the first time a channel-wise
# nn.PReLU (num_parameters > 1) runs on its "privateuseone" device:
#
#   dml_tensor_desc.cc:80] Check failed: rank <= DML_TENSOR_DIMENSION_COUNT_MAX
#
# Reproduced in isolation upstream (Microsoft Q&A, same torch/plugin
# versions: single-parameter PReLU works, num_parameters=2 aborts). Of
# our models only Real-ESRGAN Compact (realesrgan_anime_fast) uses it —
# 17 layers with 64 slopes each — which is exactly why that one model
# killed the worker on a DirectML box while the DAT2 pair and RealPLKSR
# (GELU/LeakyReLU, no PReLU) ran fine. Being an abort, nothing in-process
# can catch it, so the guard has to run BEFORE the first forward pass:
# _load_model swaps every channel-wise PReLU for the arithmetically
# identical relu(x) - w*relu(-x) below, built only from ops the other
# models already exercise on DirectML (relu, neg, mul with a [1,C,1,1]
# broadcast, sub).
#
# PROXY_SCALER_DIRECTML_PRELU picks the policy (DirectML devices only;
# every other backend ignores it):
#   patch  (default) swap the layers and stay on the GPU
#   cpu    load models that contain a channel-wise PReLU on the CPU instead
#   off    leave the model alone — reproduces the abort, for verification
DIRECTML_PRELU_ENV = "PROXY_SCALER_DIRECTML_PRELU"
DIRECTML_PRELU_POLICIES = ("patch", "cpu", "off")
DIRECTML_PRELU_DEFAULT = "patch"
# torch-directml's device type (see resolve_device); "directml" is the
# same defensive alias device_kind() accepts.
_DIRECTML_DEVICE_TYPES = ("privateuseone", "directml")


def directml_prelu_policy(environ: "os._Environ[str] | dict[str, str] | None" = None) -> str:
    """The configured policy, falling back to the default for an unset or
    unrecognized value (a typo must never silently reproduce the abort)."""
    env = os.environ if environ is None else environ
    value = (env.get(DIRECTML_PRELU_ENV) or "").strip().lower()
    return value if value in DIRECTML_PRELU_POLICIES else DIRECTML_PRELU_DEFAULT


def _is_directml_device(device: "torch.device | None") -> bool:
    return device is not None and getattr(device, "type", None) in _DIRECTML_DEVICE_TYPES


def _make_directml_safe_prelu(original):
    """A module computing exactly nn.PReLU's relu(x) + w*min(0, x) as
    relu(x) - w*relu(-x), sharing the original's weight Parameter (so it
    stays in the state dict, moves with .to(device/dtype), and a later
    _relocate_to_cpu carries it along). Defined inside a function so
    torch is only imported when a model is actually being loaded — see
    the module docstring."""
    import torch
    import torch.nn.functional as F

    class DirectMLSafePReLU(torch.nn.Module):
        def __init__(self, weight: torch.nn.Parameter) -> None:
            super().__init__()
            self.weight = weight

        @property
        def num_parameters(self) -> int:
            return self.weight.numel()

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            # Same broadcast layout torch's own prelu wrapper builds: the
            # slopes sit on dim 1 (channels) and broadcast over the rest.
            shape = [1] * x.dim()
            if x.dim() >= 2:
                shape[1] = self.weight.numel()
            w = self.weight.reshape(shape)
            return F.relu(x) - w * F.relu(-x)

        def extra_repr(self) -> str:
            return f"num_parameters={self.num_parameters} (DirectML-safe)"

    return DirectMLSafePReLU(original.weight)


def replace_channelwise_prelu(model) -> int:
    """Swap every nn.PReLU with more than one slope inside `model` for the
    DirectML-safe equivalent, in place. Returns the number swapped (0 for
    the DAT2 / RealPLKSR models, which have none). Single-parameter PReLU
    is left alone — it works on DirectML, and the workaround costs three
    extra elementwise passes per layer."""
    import torch

    targets = [
        name
        for name, module in model.named_modules()
        if isinstance(module, torch.nn.PReLU) and module.weight.numel() > 1
    ]
    for name in targets:
        parent_name, _, child = name.rpartition(".")
        parent = model.get_submodule(parent_name) if parent_name else model
        original = getattr(parent, child)
        # Module.__setattr__ registers into parent._modules[child], which is
        # also how nn.ModuleList (Compact's `body`) stores its entries.
        setattr(parent, child, _make_directml_safe_prelu(original))
    return len(targets)


def has_channelwise_prelu(model) -> bool:
    import torch

    return any(
        isinstance(m, torch.nn.PReLU) and m.weight.numel() > 1
        for m in model.modules()
    )


def apply_directml_prelu_policy(model, device: "torch.device", *, policy: str | None = None) -> "torch.device":
    """Decide how a freshly loaded model meets a DirectML device. Returns
    the device the model should actually be placed on (unchanged unless
    the "cpu" policy applies), mutating `model` under the default "patch"
    policy. A no-op — no print, no change — for every other backend and
    for models without a channel-wise PReLU."""
    import torch

    if not _is_directml_device(device) or not has_channelwise_prelu(model):
        return device
    policy = policy or directml_prelu_policy()
    if policy == "patch":
        swapped = replace_channelwise_prelu(model)
        print(
            f"  DirectML: swapped {swapped} channel-wise PReLU layer(s) for the "
            "relu-based equivalent (torch-directml aborts on the native op; "
            f"{DIRECTML_PRELU_ENV}=cpu|off to change this)"
        )
        return device
    if policy == "cpu":
        print(
            f"  DirectML: model uses channel-wise PReLU, which aborts torch-directml; "
            f"running it on the CPU instead ({DIRECTML_PRELU_ENV}=cpu)"
        )
        return torch.device("cpu")
    print(
        f"  WARNING: {DIRECTML_PRELU_ENV}=off — channel-wise PReLU left native on "
        f"{device}; torch-directml 0.2.5 is known to abort the process here",
        file=sys.stderr,
    )
    return device


# One-slot cache of the loaded (device-resident, dtype-converted) model
# descriptor, shared across the per-task Upscaler instances. Decks are
# homogeneous, so one slot gets a ~99% hit rate and eliminates the
# per-card disk read + PCIe transfer (~0.6s/task measured). Only the
# worker's main thread loads/runs models in-process (the finisher and
# prefetch threads never touch them), so no locking; the API process has
# its own interpreter and thus its own cache.
_MODEL_CACHE: dict[tuple, "ImageModelDescriptor"] = {}

# Allocator headroom learned from the first successful GPU pass of this
# process (reserved/allocated ratio, see Upscaler._record_allocator_headroom).
# None until then, which means _VRAM_HEADROOM_FIRST_TASK. Same
# single-thread ownership as _MODEL_CACHE, so no locking.
_OBSERVED_HEADROOM: float | None = None


def _current_headroom() -> float:
    return _OBSERVED_HEADROOM if _OBSERVED_HEADROOM is not None else _VRAM_HEADROOM_FIRST_TASK


# Passes smaller than this don't calibrate: a light model's ~0.4 GiB pass
# has a noisy reserved/allocated ratio (allocator granularity dominates)
# that would drag the heavy models' gate around for no reason.
_HEADROOM_CALIBRATION_MIN_ALLOCATED = 1024**3

# Largest allocated peak this process has seen. Reserved is a process-wide
# high-water the allocator never returns (reset_peak_memory_stats() can't
# lower it below what's currently reserved), so for a pass smaller than an
# earlier one `reserved` describes the EARLIER pass's arena and the ratio
# just scales with how much bigger that pass was. Verified live on Windows,
# where expandable_segments is unsupported and the arena really does sit at
# its high-water: after a 6.27 GiB untiled pass reserved 12.43 GiB, a
# 3.66 GiB tile-640 pass measured 3.91x and a 1.63 GiB tile-384 pass 8.77x,
# and the gate walked the ladder down to its 256 floor for good. Only a new
# allocated peak re-measures something real.
_PEAK_ALLOCATED_SEEN = 0


def _record_observed_headroom(reserved: int, allocated: int) -> None:
    """Fold a pass's peak reserved/allocated ratio (x1.15 safety) into the
    headroom later tasks gate on, never below the default 1.4x. Only passes
    that set a new allocated high-water calibrate -- see
    _PEAK_ALLOCATED_SEEN for why a smaller pass's ratio is meaningless."""
    global _OBSERVED_HEADROOM, _PEAK_ALLOCATED_SEEN
    if allocated < _HEADROOM_CALIBRATION_MIN_ALLOCATED:
        return
    if allocated < _PEAK_ALLOCATED_SEEN:
        return
    _PEAK_ALLOCATED_SEEN = allocated
    _OBSERVED_HEADROOM = max(_VRAM_HEADROOM_DEFAULT, reserved / allocated * 1.15)


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
        on_cpu_fallback: object | None = None,
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
        # choice — only then may _apply_auto_tile move it from free VRAM.
        # The base is kept separately: every task re-picks from it, so an
        # OOM step-down never pins later tasks low.
        self.tile_auto = tile_auto
        self._auto_base_tile = tile
        # Duck-typed timing_db.TimingCollector (kept untyped so this module
        # never imports timing_db, which imports db, which imports us).
        self._timings = timings
        # Zero-arg callable fired the moment a GPU→CPU OOM fallback
        # happens (model load or inference), so the worker can flag it for
        # the client immediately — not after the first slow CPU task
        # finishes. Duck-typed for the same import-cycle reason as timings.
        self._on_cpu_fallback = on_cpu_fallback
        self._descriptor: ImageModelDescriptor | None = None
        self._device: torch.device | None = None
        self._dtype: torch.dtype | None = None

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
        return descriptor

    def _probe_free_vram(self) -> int | None:
        """Free VRAM this task can count on, or None when unknowable."""
        import torch

        if self._device is None or self._device.type != "cuda":
            return None
        try:
            free_bytes, _ = torch.cuda.mem_get_info()
            # mem_get_info counts this process's allocator arenas as "used",
            # but reserved-yet-unallocated blocks are reusable for our own
            # tiles — without adding them back, the first task's warm
            # allocator (no more per-image empty_cache) would push every
            # later task below the gate and silently downgrade.
            free_bytes += torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
        except Exception:  # noqa: BLE001
            return None
        return int(free_bytes)

    def _apply_auto_tile(self, width: int, height: int) -> None:
        """Per task (mem_get_info is cheap): re-pick this task's auto tile
        from the base via the ladder — a heavy model's 384 may grow up to
        an untiled pass (lots of free VRAM) or shrink to 256 (small GPU)
        so it stays on the GPU instead of OOMing straight into the
        catastrophic CPU fallback. Image-aware: the estimate is for the
        largest padded tile this image actually produces."""
        if not self.tile_auto or self._auto_base_tile <= 0:
            return
        self.tile = self._auto_base_tile
        if self._device is None or self._device.type != "cuda":
            return
        new_tile = _choose_auto_tile(
            self._probe_free_vram(),
            _dtype_label(self._dtype),
            self._auto_base_tile,
            width=width,
            height=height,
            pad=self.tile_pad,
            headroom=_current_headroom(),
        )
        if _rung_order(new_tile) < _rung_order(self.tile):
            # Worth a line of its own: a step-down means this GPU is tight
            # on VRAM, which is the leading suspect in "why is it slow/on
            # CPU" reports.
            print(
                f"  low free VRAM: stepping tile down from {self.tile} to {new_tile}"
            )
        self.tile = new_tile

    def _next_oom_rung(self, width: int, height: int) -> int | None:
        """After an OOM at self.tile: the rung to retry on the same device,
        or None when the GPU has nothing left to offer (→ CPU relocation).
        Heavy auto rungs re-probe free VRAM and jump to whatever fits
        below the failed rung — a plain one-step walk only when the probe
        is unavailable. Light auto models get one tiled retry. Manual tile
        settings are never second-guessed."""
        if not self.tile_auto:
            return None
        if self._auto_base_tile <= 0:
            return _LIGHT_MODEL_RETRY_TILE if self.tile <= 0 else None
        fallback = _ladder_step_down(self.tile)
        if fallback is None:
            return None
        free_bytes = self._probe_free_vram()
        if free_bytes is None:
            return fallback
        estimate = _choose_auto_tile(
            free_bytes,
            _dtype_label(self._dtype),
            self._auto_base_tile,
            width=width,
            height=height,
            pad=self.tile_pad,
            headroom=_current_headroom(),
            below=self.tile,
        )
        if _rung_order(estimate) < _rung_order(self.tile):
            return estimate
        return fallback

    def _record_allocator_headroom(self) -> None:
        """Best-effort: after a successful GPU pass, learn how much the
        allocator really reserves over what it allocates, so later tasks
        gate on the observed ratio rather than the first-task guess."""
        import torch

        if self._device is None or self._device.type != "cuda":
            return
        try:
            _record_observed_headroom(
                torch.cuda.max_memory_reserved(), torch.cuda.max_memory_allocated()
            )
        except Exception:  # noqa: BLE001
            pass

    def _reset_peak_memory_stats(self) -> None:
        import torch

        if self._device is None or self._device.type != "cuda":
            return
        try:
            torch.cuda.reset_peak_memory_stats()
        except Exception:  # noqa: BLE001
            pass

    def _load_model(self) -> ImageModelDescriptor:
        import torch
        from spandrel import ImageModelDescriptor, ModelLoader

        device = resolve_device()
        weights = ensure_weights(self.model_id, self.scale, self.weights_dir)
        descriptor = ModelLoader().load_from_file(str(weights))
        if not isinstance(descriptor, ImageModelDescriptor):
            raise TypeError(f"Unexpected model type for {weights}")
        # Must precede the first forward pass AND the .to(device) below —
        # under the "cpu" policy the model never touches the GPU at all.
        # Deliberately not routed through _notify_cpu_fallback: that hook
        # raises the client's "GPU ran out of memory, cancel the queue?"
        # dialog, and this is a known per-model routing, not an OOM.
        if _is_directml_device(device):
            device = apply_directml_prelu_policy(descriptor.model, device)
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
                self._notify_cpu_fallback()
                _clear_device_cache(device)
                descriptor = descriptor.to(torch.device("cpu")).eval()
            else:
                raise
        return descriptor

    def _notify_cpu_fallback(self) -> None:
        """Fire the caller's fallback hook, if any — best-effort, a broken
        hook must never break the generation it's reporting on."""
        if self._on_cpu_fallback is None:
            return
        try:
            self._on_cpu_fallback()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001
            print(f"warning: cpu-fallback hook failed: {exc}", file=sys.stderr)

    def _relocate_to_cpu(self) -> ImageModelDescriptor:
        """Move loaded weights to CPU after a GPU OOM (stays on CPU afterward)."""
        import torch

        assert self._descriptor is not None
        old = self._device
        _clear_device_cache(old)
        print(f"Falling back to CPU upscale (was {old})…")
        self._notify_cpu_fallback()
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

    def _try_gpu_inference(
        self,
        descriptor: ImageModelDescriptor,
        tensor: torch.Tensor,
    ) -> "torch.Tensor | None":
        """One inference attempt on the current device; None means a GPU
        OOM. Anything else — and any error at all on CPU — propagates.
        Deliberately its own frame: the caught exception's traceback pins
        the failed pass's frames and every activation tensor they hold,
        so it must be gone (this method returned) before a retry clears
        the cache — see the caller."""
        try:
            return self._run_inference(descriptor, tensor)
        except Exception as exc:
            if (
                self._device is None
                or self._device.type == "cpu"
                or not _is_oom_error(exc)
            ):
                raise
        return None

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
            width, height = rgb.size
            self._apply_auto_tile(width, height)
            # The one line a user report needs: which capability gates fired.
            print(f"  inference config: {_dtype_label(self._dtype)}, tile {self.tile or 'off'}")
            dtype = self._dtype or torch.float32
            tensor = to_tensor(rgb).unsqueeze(0).to(self._device, dtype)
            self._reset_peak_memory_stats()

            try:
                # The inference phase deliberately spans the OOM retries,
                # so inference_s is the true wall-clock cost including any
                # fallback (tile downgrade or model relocation + re-run).
                with self._phase("inference"):
                    # Every retry below runs OUTSIDE the except block that
                    # caught the OOM, on purpose: while an except clause is
                    # active the exception (and its traceback) is alive,
                    # and the traceback pins the failed forward pass's
                    # frames — with every activation tensor they hold. Kept
                    # alive like that, _clear_device_cache() can't release
                    # a byte and every smaller rung OOMs too, all the way
                    # to the CPU. Verified live: an untiled OOM with 5 GiB
                    # still free walked the whole ladder into CPU inference
                    # when retried from inside the handler.
                    out_gpu = self._try_gpu_inference(descriptor, tensor)
                    if out_gpu is None:
                        # The VRAM-probed tile turned out not to fit after
                        # all (something else grabbed VRAM, or the estimate
                        # was off) — retry on the SAME device at whatever
                        # rung the re-probe says fits before resorting to
                        # the catastrophic CPU path. _next_oom_rung owns
                        # the policy (heavy ladder / light single retry /
                        # manual never second-guessed).
                        while True:
                            rung = self._next_oom_rung(width, height)
                            if rung is None:
                                break
                            print(
                                f"Upscale OOM on {self._device} at tile "
                                f"{self.tile or 'off'}; retrying at tile {rung}…"
                            )
                            self.tile = rung
                            _clear_device_cache(self._device)
                            out_gpu = self._try_gpu_inference(descriptor, tensor)
                            if out_gpu is not None:
                                break
                    if out_gpu is None:
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
                self._record_allocator_headroom()
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
    partial PNG. compress_level 3: this is an internal cache file, so
    encode speed beats the ~10-15% size cost — and now that non-forced
    sibling DPI tasks read it back (see task.force / pipeline.process_task),
    the lighter compression also makes those cache-hit decodes cheaper."""
    atomic_save_png(image, path, compress_level=compress_level)
    write_cache_device(path, device)


def cache_stem(scryfall_id: str | None, custom_hash: str | None = None) -> str:
    """Filename-safe token identifying one face in the cache directories.

    Deliberately NOT customs.identity_key(): that yields 'custom:<sha256>'
    and a colon is not a legal filename character on Windows, which the
    desktop app ships to. An underscore separator gives the same
    collision-freedom — a Scryfall UUID can never start with 'custom_'.
    """
    if custom_hash:
        return f"custom_{custom_hash}"
    if not scryfall_id:
        raise ValueError("A cached face needs either a scryfall_id or a custom_hash.")
    return scryfall_id


def cache_path(
    cache_dir: Path,
    scryfall_id: str | None,
    face_index: int | None,
    scale: int,
    model: UpscaleModel | str,
    *,
    custom_hash: str | None = None,
) -> Path:
    model_id = parse_model(model)
    face_part = "single" if face_index is None else f"face{face_index}"
    stem = cache_stem(scryfall_id, custom_hash)
    return cache_dir / f"{stem}_{face_part}_{model_id.value}_x{scale}.png"


def original_cache_path(
    cache_dir: Path,
    scryfall_id: str | None,
    face_index: int | None,
    *,
    custom_hash: str | None = None,
) -> Path:
    face_part = "single" if face_index is None else f"face{face_index}"
    stem = cache_stem(scryfall_id, custom_hash)
    return cache_dir / "originals" / f"{stem}_{face_part}.png"


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
    scryfall_id: str | None,
    face_index: int | None,
    force: bool = False,
    timings: object | None = None,
    defer_cache_write: bool = False,
    custom_hash: str | None = None,
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
        custom_hash=custom_hash,
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
            from_cache=True,
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
