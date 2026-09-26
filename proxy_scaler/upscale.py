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


class Backend(str, Enum):
    """Which inference runtime a model runs on. Data on the model, never a
    class: pipeline.py picks the Upscaler implementation from this, so a
    model can move between runtimes (a DAT2 that later converts to ncnn,
    say) by editing one dict entry."""

    TORCH = "torch"  # PyTorch + spandrel: CUDA / ROCm / MPS / DirectML / CPU
    NCNN = "ncnn"  # ncnn under Vulkan (any vendor's GPU), see ncnn_backend.py
    # ONNX Runtime's WebGPU provider (onnx_backend.py): Dawn under Vulkan
    # on Linux, Direct3D 12 on Windows (never DirectML). Runs the DAT
    # models ncnn can't (their attention needs 5-6 D tensors).
    ONNX = "onnx"


def gpu_api_label() -> str:
    """The graphics API the any-GPU models run on here, for labels that
    must be literally true: ONNX Runtime WebGPU's Windows build only has
    Direct3D 12 compiled in, its Linux build only Vulkan (checked in the
    shipped packages). ncnn is Vulkan everywhere (MoltenVK on macOS)."""
    return "DirectX 12" if sys.platform == "win32" else "Vulkan"


def model_group(backend: Backend) -> str:
    """Dropdown header for a backend, served by GET /api/models so every
    client groups the same way. The ncnn and ONNX models share one group;
    on Windows it can't be called "Vulkan" because the ONNX ones aren't."""
    if backend is Backend.TORCH:
        return "Models"
    return "GPU-Universal Models" if sys.platform == "win32" else "Vulkan Models"


class UpscaleModel(str, Enum):
    REALESRGAN_ANIME_FAST = "realesrgan_anime_fast"
    ILLUSTRATIONJANAI = "illustrationjanai"
    ULTRASHARP_V2 = "ultrasharp_v2"
    # NOTE for future members: db.py's filename-slug regex sorts enum
    # values longest-first, which is the only thing making a value that
    # prefixes another (like this one vs ultrasharp_v2) safe.
    ULTRASHARP_V2_LITE = "ultrasharp_v2_lite"
    # --- ncnn (Vulkan) models. Ids end in _vk; the same longest-first
    # slug rule keeps realesrgan_anime_fast_vk apart from its torch twin.
    # Weights are ncnn .param/.bin pairs (see _WEIGHTS), the runtime is
    # ncnn_backend.NcnnUpscaler, tiling comes from NCNN_TILE_PRESETS.
    REALESRGAN_ANIME_FAST_VK = "realesrgan_anime_fast_vk"
    ANIMESHARP_VK = "animesharp_vk"
    ILLUSTRATIONJANAI_ESRGAN_VK = "illustrationjanai_esrgan_vk"
    # --- ONNX Runtime (WebGPU) models: the DAT pair on any GPU. Ids name
    # the runtime, not a graphics API — they reach output filenames and
    # the Tasks table, and the API differs by OS (see gpu_api_label).
    ULTRASHARP_V2_ORT = "ultrasharp_v2_ort"
    ILLUSTRATIONJANAI_ORT = "illustrationjanai_ort"

    @property
    def backend(self) -> Backend:
        """Same all-members-dict style as label: a member missing here is a
        KeyError at first use, never a silent default runtime."""
        return {
            UpscaleModel.REALESRGAN_ANIME_FAST: Backend.TORCH,
            UpscaleModel.ILLUSTRATIONJANAI: Backend.TORCH,
            UpscaleModel.ULTRASHARP_V2: Backend.TORCH,
            UpscaleModel.ULTRASHARP_V2_LITE: Backend.TORCH,
            UpscaleModel.REALESRGAN_ANIME_FAST_VK: Backend.NCNN,
            UpscaleModel.ANIMESHARP_VK: Backend.NCNN,
            UpscaleModel.ILLUSTRATIONJANAI_ESRGAN_VK: Backend.NCNN,
            UpscaleModel.ULTRASHARP_V2_ORT: Backend.ONNX,
            UpscaleModel.ILLUSTRATIONJANAI_ORT: Backend.ONNX,
        }[self]

    @property
    def group(self) -> str:
        return model_group(self.backend)

    @property
    def short_label(self) -> str:
        """Compact badge text (deck-list chips, thumbnail labels). Served by
        GET /api/models so it can be per-OS; the frontend keeps its own
        copy only as a fallback for older servers."""
        api = "DX" if gpu_api_label() == "DirectX 12" else "VK"
        return {
            UpscaleModel.REALESRGAN_ANIME_FAST: "REAF",
            UpscaleModel.ILLUSTRATIONJANAI: "IJ",
            UpscaleModel.ULTRASHARP_V2: "USV2",
            UpscaleModel.ULTRASHARP_V2_LITE: "USV2 Lite",
            UpscaleModel.REALESRGAN_ANIME_FAST_VK: "REAF-VK",
            UpscaleModel.ANIMESHARP_VK: "AS-VK",
            UpscaleModel.ILLUSTRATIONJANAI_ESRGAN_VK: "IJE-VK",
            UpscaleModel.ULTRASHARP_V2_ORT: f"USV2-{api}",
            UpscaleModel.ILLUSTRATIONJANAI_ORT: f"IJ-{api}",
        }[self]

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
            UpscaleModel.REALESRGAN_ANIME_FAST_VK: (
                "Real-ESRGAN Anime Fast (Vulkan) (the same compact model, run through ncnn on any GPU)"
            ),
            UpscaleModel.ANIMESHARP_VK: "AnimeSharp (Vulkan) (ESRGAN, anime and line art, strong on text)",
            UpscaleModel.ILLUSTRATIONJANAI_ESRGAN_VK: (
                "IllustrationJaNai ESRGAN (Vulkan) (the ESRGAN sibling of IllustrationJaNai: illustrations, digital art, manga covers)"
            ),
            UpscaleModel.ULTRASHARP_V2_ORT: (
                f"UltraSharpV2 ({gpu_api_label()}) (the same model on any GPU, full precision)"
            ),
            UpscaleModel.ILLUSTRATIONJANAI_ORT: (
                f"IllustrationJaNai ({gpu_api_label()}) (the same model on any GPU, full precision)"
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
            UpscaleModel.REALESRGAN_ANIME_FAST_VK: "Fastest",
            UpscaleModel.ANIMESHARP_VK: "Balanced",
            UpscaleModel.ILLUSTRATIONJANAI_ESRGAN_VK: "Balanced",
            UpscaleModel.ULTRASHARP_V2_ORT: "Best quality — slowest",
            UpscaleModel.ILLUSTRATIONJANAI_ORT: "Best for illustrations — slowest",
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
    explicit non-zero setting always wins, regardless of model.

    ncnn models are never in HEAVY_MODELS: the torch VRAM ladder can't see
    a Vulkan device, so their 0 is resolved by NcnnUpscaler itself from
    NCNN_TILE_PRESETS (the medium preset), never an untiled pass."""
    if tile_size_setting > 0:
        return tile_size_setting
    return DEFAULT_TILE_SIZE if model in HEAVY_MODELS else 0


NCNN_MODELS = frozenset(m for m in UpscaleModel if m.backend is Backend.NCNN)


@dataclass(frozen=True)
class TilePreset:
    """One entry of the Vulkan models' "GPU VRAM" dropdown."""

    key: str
    label: str
    tile: int


# The "GPU VRAM" dropdown every model shows instead of a raw tile number.
# The tiers are shared: the number behind each is the tile that fits an
# x4 pass on a card of that size with headroom. The client writes the
# chosen tile straight into the existing tile_size setting, so nothing
# new travels through the API or the DB.
#
# torch models get an extra "Auto" tier (tile 0): the torch path probes
# free CUDA memory per task and picks the tile itself (_apply_auto_tile),
# which is what tile_size 0 has always meant there. ncnn's Python binding
# exposes no VRAM figure, so Vulkan models have no Auto: their default is
# the medium tier, and a failed pass steps down the tiers (largest first)
# before falling to the CPU. Tile numbers are provisional until the bake-off.
VRAM_TIERS: tuple[TilePreset, ...] = (
    TilePreset("low", "Low VRAM (4 GB or less)", 128),
    TilePreset("medium", "Medium VRAM (6–8 GB)", 256),
    TilePreset("high", "High VRAM (12 GB+)", 384),
    TilePreset("max", "Max VRAM (16 GB+)", 512),
)
AUTO_TILE_PRESET = TilePreset("auto", "Auto (measure free VRAM)", 0)
TORCH_TILE_PRESETS: tuple[TilePreset, ...] = (AUTO_TILE_PRESET,) + VRAM_TIERS
NCNN_TILE_PRESETS: tuple[TilePreset, ...] = VRAM_TIERS
TORCH_DEFAULT_PRESET = "auto"
NCNN_DEFAULT_PRESET = "medium"


# ONNX (DAT) exports are fixed-size: DAT's padding and attention masks are
# computed from the input size at trace time, so a file only runs at the
# size it was exported at (a 64 px export failed at 96 px). One file per
# tier. A preset's `tile` is only the tier's key here (it is what the
# client stores in tile_size); the file's input shape comes from
# onnx_input_shape(), and host_tiling.run_fixed_tiled spaces those tiles
# evenly across the image with at least 2x ONNX_TILE_PAD of overlap, so
# every kept pixel has the same context as the torch path's pad.
#
# Shapes are multiples of 32 (DAT pads its attention input to its 32 px
# window; anything else is computed and thrown away) and picked to cover
# the standard 745x1040 card with the least total work: Low 6x8 tiles of
# 192x192, Medium 4x6 of 256x256, High 2x4 of 416x320 (a square 384 needs
# 12 tiles for 40% more work). Peak VRAM, UltraSharpV2 on WebGPU (3080 Ti,
# storage buffer cache "simple"): 192x192 2.5 GB, 256x256 4.3 GB, 416x320
# 8.6 GB. The peak is transient: memory measured after a run reads about
# half of it. Larger tiles run out of device memory on a 12 GB card, so
# there is no Max tier.
ONNX_TILE_PAD = 32
ONNX_TILE_PRESETS: tuple[TilePreset, ...] = (
    TilePreset("low", "Low VRAM (4 GB or less)", 128),
    TilePreset("medium", "Medium VRAM (6–8 GB)", 192),
    TilePreset("high", "High VRAM (12 GB+)", 320),
)
ONNX_DEFAULT_PRESET = "medium"
# tier key -> exported input (height, width)
_ONNX_INPUT_SHAPES: dict[int, tuple[int, int]] = {
    128: (192, 192),
    192: (256, 256),
    320: (320, 416),
}


def onnx_input_shape(tile: int) -> tuple[int, int]:
    """(height, width) of the file a tier runs (see ONNX_TILE_PRESETS)."""
    return _ONNX_INPUT_SHAPES[tile]


def tile_presets_for(model: UpscaleModel) -> tuple[TilePreset, ...]:
    """The tiers a model's dropdown offers, per backend."""
    return {
        Backend.TORCH: TORCH_TILE_PRESETS,
        Backend.NCNN: NCNN_TILE_PRESETS,
        Backend.ONNX: ONNX_TILE_PRESETS,
    }[model.backend]


def default_tile_preset(model: UpscaleModel) -> TilePreset | None:
    key = {
        Backend.TORCH: TORCH_DEFAULT_PRESET,
        Backend.NCNN: NCNN_DEFAULT_PRESET,
        Backend.ONNX: ONNX_DEFAULT_PRESET,
    }[model.backend]
    for preset in tile_presets_for(model):
        if preset.key == key:
            return preset
    return None


@dataclass(frozen=True)
class WeightFile:
    filename: str
    url: str
    # Hex SHA-256, verified after download and on every later load. None
    # only for the original torch entries, whose upstream hosts we don't
    # control; everything we host ourselves carries one.
    sha256: str | None = None


@dataclass(frozen=True)
class _WeightSpec:
    """Every file a model needs on disk. One for the torch models (a .pth
    or .safetensors), a .param + .bin pair for ncnn."""

    files: tuple[WeightFile, ...]

    @property
    def primary(self) -> WeightFile:
        return self.files[0]

    @property
    def filename(self) -> str:
        return self.primary.filename


def _single(filename: str, url: str) -> _WeightSpec:
    return _WeightSpec((WeightFile(filename, url),))


# Where our own conversions and mirrors of ncnn model files live. The
# on-disk name is <model.value>.param / .bin so the two always pair up
# and the id alone names the model. Versioned prefix: a re-conversion goes
# to v2/, never silently changes a hash under an installed client.
NCNN_WEIGHTS_BASE_URL = "https://dl.proxy-scaler.com/models/ncnn/v1/"


# v2: GlobalAveragePool rewritten to ReduceMean (ORT WebGPU's pool kernel
# was 23% of the run) and the 416x320 High tier. v1 stays on the bucket
# for the test build that shipped with it.
ONNX_WEIGHTS_BASE_URL = "https://dl.proxy-scaler.com/models/onnx/v2/"


def onnx_filename(model: UpscaleModel, shape: tuple[int, int]) -> str:
    h, w = shape
    return f"{model.value}_{w}x{h}.onnx"


def _onnx_tiers(model: UpscaleModel, sha256_by_file: dict[str, str]) -> _WeightSpec:
    """One fixed-size .onnx per VRAM tier (see ONNX_TILE_PRESETS), in tier
    order, keyed by filename. A task downloads only the file for the tier
    it runs at."""
    files = []
    for preset in ONNX_TILE_PRESETS:
        name = onnx_filename(model, onnx_input_shape(preset.tile))
        files.append(WeightFile(name, f"{ONNX_WEIGHTS_BASE_URL}{name}", sha256_by_file[name]))
    return _WeightSpec(tuple(files))


def _ncnn_pair(model: UpscaleModel, param_sha256: str, bin_sha256: str) -> _WeightSpec:
    return _WeightSpec(
        (
            WeightFile(f"{model.value}.param", f"{NCNN_WEIGHTS_BASE_URL}{model.value}.param", param_sha256),
            WeightFile(f"{model.value}.bin", f"{NCNN_WEIGHTS_BASE_URL}{model.value}.bin", bin_sha256),
        )
    )


# Official release weights loadable by Spandrel, plus the ncnn pairs.
_WEIGHTS: dict[tuple[UpscaleModel, int], _WeightSpec] = {
    # Official release — the "Compact" (SRVGGNetCompact) architecture,
    # much smaller/faster than the RRDBNet-based anime models.
    (UpscaleModel.REALESRGAN_ANIME_FAST, 4): _single(
        "realesr-animevideov3.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth",
    ),
    # CC-BY-NC-SA-4.0 (non-commercial). Original author hosts on Google
    # Drive only, which our simple streaming downloader can't handle for
    # files this size — using a third-party HuggingFace mirror instead.
    # If this mirror ever disappears, search for
    # "4x_IllustrationJaNai_V1_DAT2" on huggingface.co for a replacement.
    (UpscaleModel.ILLUSTRATIONJANAI, 4): _single(
        "4x_IllustrationJaNai_V1_DAT2_190k.pth",
        "https://huggingface.co/tomjackson2023/upscale_models/resolve/main/4x_IllustrationJaNai_V1_DAT2_190k.pth",
    ),
    # CC-BY-NC-SA-4.0 (non-commercial). Officially hosted by the creator.
    (UpscaleModel.ULTRASHARP_V2, 4): _single(
        "4x-UltraSharpV2.safetensors",
        "https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2.safetensors",
    ),
    # The author's own companion release to UltraSharpV2, from the same
    # repo under the same CC-BY-NC-SA-4.0 license. RealPLKSR architecture:
    # a small CNN (~30MB weights vs 140MB) that sits between the compact
    # video model and the DAT2 transformers on both speed and quality —
    # light enough to run untiled (not in HEAVY_MODELS).
    (UpscaleModel.ULTRASHARP_V2_LITE, 4): _single(
        "4x-UltraSharpV2_Lite.safetensors",
        "https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2_Lite.safetensors",
    ),
    # The official ncnn conversion of realesr-animevideov3 (x4), byte-for-
    # byte from Real-ESRGAN's realesrgan-ncnn-vulkan-20220424 release
    # (BSD-3), re-hosted under our name so the id alone finds it.
    (UpscaleModel.REALESRGAN_ANIME_FAST_VK, 4): _ncnn_pair(
        UpscaleModel.REALESRGAN_ANIME_FAST_VK,
        "850a248e7c14c27e5bd8cf7265113a9441036a7db63963bb8aa5169d788a435e",
        "548a36f9c3f4ab8da56cd3b13badf23968bee207b396dad14d04b830e5f2ab2d",
    ),
    # Our esrgan2ncnn conversion of Kim2091's 4x-AnimeSharp (CC-BY-NC-SA-4.0).
    (UpscaleModel.ANIMESHARP_VK, 4): _ncnn_pair(
        UpscaleModel.ANIMESHARP_VK,
        "d501e5d13beda4ee579aad0bacd8b420ae0e3ab96f38f37e8c03655b99715bd8",
        "7e63c002ad4a410fd7e81c539f81915a1046fbfea63fc4d5a3500a101ca0f6c1",
    ),
    # Our esrgan2ncnn conversion of the-database's 4x_IllustrationJaNai_V1_ESRGAN_135k
    # (CC-BY-NC-SA-4.0) — same author and dataset as the DAT2 model above.
    (UpscaleModel.ILLUSTRATIONJANAI_ESRGAN_VK, 4): _ncnn_pair(
        UpscaleModel.ILLUSTRATIONJANAI_ESRGAN_VK,
        "d501e5d13beda4ee579aad0bacd8b420ae0e3ab96f38f37e8c03655b99715bd8",
        "1d5f792cd58cf31213193467b0ab5f2ff9a2e3de63d3bf8caa8abe8eebe95df5",
    ),
    # ONNX (WebGPU) exports of the two DAT models, one fixed-size file per
    # VRAM tier (packaging/onnx/export-models.py; gated at 85-89 dB against
    # PyTorch fp32 on a real card crop). CC-BY-NC-SA-4.0, like their sources.
    (UpscaleModel.ULTRASHARP_V2_ORT, 4): _onnx_tiers(
        UpscaleModel.ULTRASHARP_V2_ORT, {
            "ultrasharp_v2_ort_192x192.onnx": "fa9d3bfc784bfb09d9799c3a93175a8cb661d4310c24b715662381002c0ea798",
            "ultrasharp_v2_ort_256x256.onnx": "453a8e2c22a13a7099dc224ff1c9d23ce70b88fed3f47db714edde496432c96f",
            "ultrasharp_v2_ort_416x320.onnx": "dd9b712957bdc39255eb9a780d72106b2ca4c5efb64b1e6ca586f23c3adba355",
        }
    ),
    (UpscaleModel.ILLUSTRATIONJANAI_ORT, 4): _onnx_tiers(
        UpscaleModel.ILLUSTRATIONJANAI_ORT, {
            "illustrationjanai_ort_192x192.onnx": "db7dec8a4bb616af6a34043d0d69d7e9529cc21efed2d6a8e0e09e3f85ade28c",
            "illustrationjanai_ort_256x256.onnx": "4e81468c2318424ba80a535cefff93e672f44496f6df6b8565915db1e26d383b",
            "illustrationjanai_ort_416x320.onnx": "2c4c5c79a45481e8882f6addc480151eb2a9587b0d3c08af78d03d2c9d57501b",
        }
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
            _apply_directml_tiled_resources_policy(torch_directml)
            return torch_directml.device()
    return torch.device("cpu")


# PROXY_SCALER_DIRECTML_TILED_RESOURCES=off makes torch-directml back
# tensors with plain committed D3D12 resources instead of its default
# "tiled resources" (memory mapped in pages). Diagnostic knob, default
# unchanged: on an RX 9070 XT, tiles cut from sub-regions of a larger
# image read/wrote the wrong memory, and a page-mapping fault is one
# candidate. Undocumented plugin API, so every failure here is ignored.
DIRECTML_TILED_RESOURCES_ENV = "PROXY_SCALER_DIRECTML_TILED_RESOURCES"
_DIRECTML_TILED_RESOURCES_APPLIED = False


def _apply_directml_tiled_resources_policy(torch_directml) -> None:
    global _DIRECTML_TILED_RESOURCES_APPLIED
    if _DIRECTML_TILED_RESOURCES_APPLIED:
        return
    _DIRECTML_TILED_RESOURCES_APPLIED = True
    value = (os.environ.get(DIRECTML_TILED_RESOURCES_ENV) or "").strip().lower()
    if value not in ("off", "0", "false", "disable", "disabled"):
        return
    try:
        torch_directml.disable_tiled_resources(True)
        print(f"  directml: tiled resources disabled ({DIRECTML_TILED_RESOURCES_ENV}={value})")
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not disable DirectML tiled resources: {exc}", file=sys.stderr)


# Set when a DirectML pass hit a device error (out of memory, or an error
# whose message couldn't even be decoded). torch-directml has no way to
# hand its memory pool back, so after one of those the worker process
# keeps holding VRAM and later cards fail too; the worker checks this
# after each task and exits with WORKER_RECYCLE_EXIT_CODE so the
# supervisor starts a fresh one (the OS frees everything on exit).
# Single-thread ownership as _MODEL_CACHE.
_WORKER_RECYCLE_REASON: str | None = None


def request_worker_recycle(reason: str) -> None:
    global _WORKER_RECYCLE_REASON
    if _WORKER_RECYCLE_REASON is None:
        _WORKER_RECYCLE_REASON = reason


def worker_recycle_reason() -> str | None:
    return _WORKER_RECYCLE_REASON


def _lost_directml_message(exc: BaseException) -> str | None:
    """The original text of a DirectML error that arrived as a
    UnicodeDecodeError, else None.

    On a non-English Windows the driver's error text is in the system code
    page (cp1252 on a French install: 0x92 is ’, 0xE9 is é), and
    torch-directml decodes it as UTF-8 — so the out-of-memory error
    surfaced as "'utf-8' codec can't decode byte 0x92 in position 1",
    invisible to _is_oom_error. The undecodable bytes are still on the
    exception; decode them the way Windows wrote them."""
    if not isinstance(exc, UnicodeDecodeError):
        return None
    raw = exc.object if isinstance(exc.object, (bytes, bytearray)) else bytes(exc.object)
    import locale

    for encoding in (locale.getpreferredencoding(False), "cp1252", "latin-1"):
        try:
            return bytes(raw).decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return bytes(raw).decode("latin-1", "replace")


def _is_device_memory_error(exc: BaseException, device: "torch.device | None") -> bool:
    """_is_oom_error, plus DirectML's undecodable errors: the only DirectML
    errors seen to arrive that way are out-of-memory ones in a non-English
    locale, and treating one as OOM costs at worst a smaller tile or the
    CPU fallback, never a lost task."""
    if _is_oom_error(exc):
        return True
    if _is_directml_device(device) and isinstance(exc, UnicodeDecodeError):
        print(f"  directml error (system-language message): {_lost_directml_message(exc)}")
        return True
    return False


def _is_oom_error(exc: BaseException) -> bool:
    import torch

    if isinstance(exc, torch.cuda.OutOfMemoryError):
        return True
    # MPS / older torch sometimes raise RuntimeError with this message
    msg = str(exc).lower()
    if "out of memory" in msg or "oom" in msg:
        return True
    # torch-directml words it differently — "There is not enough GPU video
    # memory available" (seen on an RX 9070 XT asking for an untiled
    # UltraSharpV2 pass) — or surfaces the raw HRESULT E_OUTOFMEMORY.
    # Missed, it failed the task outright instead of walking the tile
    # ladder / falling back to the CPU like every other backend does.
    if "not enough" in msg and "memory" in msg:
        return True
    return "e_outofmemory" in msg or "0x8007000e" in msg


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
    elif _is_directml_device(device):
        # torch-directml has no empty_cache()-equivalent. The best we can
        # do is make sure nothing on the Python side still references the
        # failed pass's tensors — reference cycles (tracebacks, frames)
        # otherwise keep them, and their device memory, alive until some
        # later collection, which is how a failed task left VRAM held
        # until the app was restarted.
        import gc

        gc.collect()


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
    # "vulkan" is what the ncnn backend reports (ncnn_backend.py),
    # "webgpu" the ONNX Runtime one (onnx_backend.py).
    if name in ("cuda", "mps", "gpu", "privateuseone", "directml", "vulkan", "webgpu"):
        return "gpu"
    return name or "unknown"


def device_backend(device: torch.device | str | None) -> str:
    """The *actual* backend name — "cuda" | "mps" | "privateuseone" |
    "vulkan" (ncnn) | "cpu" | "unknown" — as opposed to device_kind()'s
    deliberately coarse gpu/cpu answer.

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

# Free VRAM a later task needs to see before it moves a model that an
# earlier task's OOM parked on the CPU (_relocate_to_cpu) back onto the
# GPU. The fallback only fires once even the 256 rung fails, i.e. with
# well under 1 GiB genuinely free, so "things have clearly changed" is
# the bar: 4 GiB covers the 512 rung at steady-state headroom (3.6 GiB),
# so the returning task gets a useful tile, and sits far enough above
# the failure point that the model doesn't bounce between CPU and GPU
# while whatever squeezed the card (a game, another app) is still there.
# Observed on an RTX 5080: without this, one squeeze left every later
# UltraSharpV2 card on the CPU (~90 s vs ~6 s) until the worker restarted.
_GPU_RETURN_MIN_FREE = 4 * 1024**3


def _should_return_to_gpu(free_bytes: int | None) -> bool:
    return free_bytes is not None and free_bytes >= _GPU_RETURN_MIN_FREE


def _free_cuda_vram() -> int | None:
    """Free VRAM this process can count on right now (bytes), or None
    without CUDA / when the probe fails. mem_get_info counts this
    process's allocator arenas as "used", but reserved-yet-unallocated
    blocks are reusable for our own tiles — without adding them back, a
    warm allocator (no per-image empty_cache) would push every later task
    below the gate and silently downgrade."""
    import torch

    if not torch.cuda.is_available():
        return None
    try:
        free_bytes, _ = torch.cuda.mem_get_info()
        free_bytes += torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
    except Exception:  # noqa: BLE001
        return None
    return int(free_bytes)

# ---- Per-task allocator cap ------------------------------------------
#
# The ladder can only step down if a pass that doesn't fit actually raises
# torch's OutOfMemoryError. On Windows it doesn't: the Nvidia driver's
# default "CUDA - Sysmem Fallback Policy" lets cudaMalloc succeed out of
# system RAM once VRAM is gone, so the pass never fails — it crawls (~15x
# slower, GPU pegged, measured on an RTX 5080 laptop) with no retry. That
# is the 0.2.1 "GPU at 100% for minutes" report. torch's per-process
# memory fraction is enforced inside the caching allocator, before the
# driver is asked, so capping it at what is physically free right now
# turns the spill back into the OOM the ladder is built around. Second
# benefit: the cap bounds *reserved* memory — an allocator that can't use
# expandable_segments (again Windows: the cu128 wheel ignores it) trims
# its cache at the cap instead of growing to ~2x allocated.
#
# Recomputed per task from mem_get_info: free + this process's own arena
# (reusable by us, invisible to mem_get_info's "free") - a margin for the
# driver and other allocations. Never below _VRAM_CAP_MIN so the light
# models still get to try on a nearly-full card.
_VRAM_CAP_MARGIN = 512 * 1024**2
_VRAM_CAP_MIN = 1024**3


def _allocator_fraction(
    free_bytes: int,
    reserved_bytes: int,
    total_bytes: int,
    margin: int = _VRAM_CAP_MARGIN,
) -> float | None:
    """torch.cuda.set_per_process_memory_fraction() value that keeps this
    process's allocator inside physically available VRAM; None when the
    inputs are unusable."""
    if total_bytes <= 0:
        return None
    cap = free_bytes + reserved_bytes - margin
    cap = max(cap, min(_VRAM_CAP_MIN, total_bytes))
    return min(1.0, cap / total_bytes)


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
    dtype: str = "fp32"  # "bf16" | "fp32" | "fp16" (ncnn)
    # True when the image came from the x4 cache PNG instead of a fresh
    # model pass — the caller then knows there is nothing to write back.
    from_cache: bool = False


def ensure_weights(
    model: UpscaleModel,
    scale: int,
    weights_dir: Path,
) -> Path:
    """The model's primary weight file, downloaded if missing. Torch
    callers only ever need the one file; ncnn callers use
    ensure_weight_files for the whole .param/.bin pair."""
    paths, _ = ensure_weight_files(model, scale, weights_dir)
    return paths[0]


def ensure_weight_files(
    model: UpscaleModel,
    scale: int,
    weights_dir: Path,
) -> tuple[list[Path], bool]:
    """Every file in the model's _WeightSpec, on disk and verified.
    Returns (paths in spec order, whether anything was downloaded) — the
    flag lets a caller attribute model_load time to a real fetch.

    A file with a known hash is re-hashed on every call (the ncnn files
    are a few MB; the torch entries carry no hash and skip this) so a
    truncated or tampered copy is re-fetched once instead of loading as
    garbage. A fresh download that fails its hash is deleted and raised:
    either the transfer broke or the hosted file changed, and neither
    should quietly become the model users run."""
    if scale not in model.supported_scales:
        raise ValueError(
            f"{model.value} supports scales {model.supported_scales}, not x{scale}"
        )
    spec = _WEIGHTS[(model, scale)]
    paths: list[Path] = []
    downloaded = False
    for wf in spec.files:
        path, fetched = _ensure_one(wf, Path(weights_dir))
        downloaded = downloaded or fetched
        paths.append(path)
    return paths, downloaded


def ensure_weight_file(
    model: UpscaleModel,
    scale: int,
    weights_dir: Path,
    filename: str,
) -> tuple[Path, bool]:
    """One named file out of the model's _WeightSpec — the ONNX models keep
    a file per VRAM tier and a task needs only the one it runs at (~53 MB
    each). Same verify/re-fetch rules as ensure_weight_files."""
    spec = _WEIGHTS[(model, scale)]
    for wf in spec.files:
        if wf.filename == filename:
            return _ensure_one(wf, Path(weights_dir))
    raise KeyError(f"{model.value} has no weight file {filename!r}")


def _ensure_one(wf: WeightFile, weights_dir: Path) -> tuple[Path, bool]:
    weights_dir.mkdir(parents=True, exist_ok=True)
    path = weights_dir / wf.filename
    if path.exists() and path.stat().st_size > 0:
        if wf.sha256 is None or _sha256_of(path) == wf.sha256:
            return path, False
        print(f"{wf.filename} failed its checksum; re-downloading …")
        path.unlink()
    _download_weight_file(wf, path)
    return path, True


def _sha256_of(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_weight_file(wf: WeightFile, path: Path) -> None:
    import hashlib

    print(f"Downloading {wf.filename} …")
    resp = requests.get(wf.url, timeout=120, stream=True)
    resp.raise_for_status()
    tmp = path.with_suffix(path.suffix + ".part")
    digest = hashlib.sha256()
    with tmp.open("wb") as fp:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fp.write(chunk)
                digest.update(chunk)
    if wf.sha256 is not None and digest.hexdigest() != wf.sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"{wf.filename}: checksum mismatch after download "
            f"(expected {wf.sha256[:12]}…, got {digest.hexdigest()[:12]}…) — "
            "the transfer was corrupted or the hosted file changed"
        )
    tmp.replace(path)
    print(f"Saved weights to {path}")


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


# A real x4 pass box-downscaled back to the input size sits ~30 dB from
# it; the black and noise tiles DirectML produced sit near 10 dB. Low on
# purpose so strong sharpening never trips it.
DIRECTML_TILE_MIN_PSNR_DB = 18.0


def _directml_tile_problem(out: "torch.Tensor", tile_in: "torch.Tensor", scale: int) -> str | None:
    """None when a DirectML tile output looks like an upscale of its
    input, else a short reason for the log. Both CPU float32, NCHW."""
    import torch
    import torch.nn.functional as F

    _, c, h, w = tile_in.shape
    if tuple(out.shape) != (1, c, h * scale, w * scale):
        return f"with shape {tuple(out.shape)}"
    if not bool(torch.isfinite(out).all()):
        return "non-finite (NaN/inf)"
    down = F.avg_pool2d(out.clamp(0.0, 1.0), scale)
    mse = float(torch.mean((down - tile_in) ** 2))
    if mse > 0.0:
        psnr = 10.0 * float(torch.log10(torch.tensor(1.0 / mse)))
        if psnr < DIRECTML_TILE_MIN_PSNR_DB:
            return f"implausible ({psnr:.1f} dB from its input)"
    return None

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
        # frees its VRAM before the new one settles in. The slot spans
        # both runtimes — a Vulkan net loaded earlier goes too (small,
        # but the symmetry keeps the accounting simple; ncnn_backend does
        # the same to us before it loads).
        clear_model_cache()
        from .ncnn_backend import clear_ncnn_cache
        from .onnx_backend import clear_onnx_cache

        clear_ncnn_cache()
        clear_onnx_cache()
    _MODEL_CACHE[key] = descriptor


def make_upscaler(model: UpscaleModel | str, **kwargs):
    """The right Upscaler implementation for a model's backend, same
    constructor kwargs either way. pipeline._upscalers_for_targets does
    this dispatch inline (so its Upscaler/NcnnUpscaler names stay
    monkeypatchable); this is for the CLI and direct callers."""
    model_id = parse_model(model)
    if model_id.backend is Backend.NCNN:
        from .ncnn_backend import NcnnUpscaler

        return NcnnUpscaler(model_id, **kwargs)
    if model_id.backend is Backend.ONNX:
        from .onnx_backend import OnnxUpscaler

        return OnnxUpscaler(model_id, **kwargs)
    return Upscaler(model_id, **kwargs)


def backend_available(backend: Backend) -> bool:
    """Whether this build can run a backend at all. torch and ncnn ship in
    every build; ONNX Runtime WebGPU exists for Linux and Windows only
    (no macOS package), so its models are hidden where it's missing.
    Cached per process by onnx_backend."""
    if backend is Backend.ONNX:
        from .onnx_backend import onnx_available

        return onnx_available()
    return True


def model_available(model: UpscaleModel) -> bool:
    return backend_available(model.backend)


class Upscaler:
    """Lazy-loaded Spandrel upscaler for a chosen model + scale (torch
    backend only — see make_upscaler / ncnn_backend.NcnnUpscaler)."""

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
        if self.model_id.backend is not Backend.TORCH:
            raise TypeError(
                f"{self.model_id.value} runs on the {self.model_id.backend.value} "
                "backend; construct it via make_upscaler() (or NcnnUpscaler)"
            )
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

        param = next(descriptor.model.parameters())
        if param.device.type == "cpu":
            # A cached descriptor on the CPU is (almost always) one that
            # _relocate_to_cpu() parked there after an OOM in an earlier
            # task. That was the right call then; it shouldn't be forever.
            # CUDA: only once the free-VRAM probe says it fits. DirectML
            # has no probe, so it always tries again: without this, one
            # oversized tile early in a session parked the model on the
            # CPU for every later card until the app was restarted. A task
            # that still doesn't fit just falls back again.
            target = resolve_device()
            if (target.type == "cuda" and _should_return_to_gpu(_free_cuda_vram())) or (
                _is_directml_device(target)
            ):
                descriptor = self._return_to_gpu(descriptor, target)
                param = next(descriptor.model.parameters())
        # Per-instance state is derived from the descriptor's own weights,
        # hit or miss: a shared descriptor that _relocate_to_cpu() moved to
        # CPU/fp32 in a previous task (and _return_to_gpu() may just have
        # moved back) is then described truthfully instead of through
        # stale cached metadata.
        self._device = param.device
        self._dtype = (
            torch.bfloat16 if param.dtype == torch.bfloat16 else torch.float32
        )
        self._descriptor = descriptor
        return descriptor

    def _probe_free_vram(self) -> int | None:
        """Free VRAM this task can count on, or None when unknowable."""
        if self._device is None or self._device.type != "cuda":
            return None
        return _free_cuda_vram()

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

    def _cap_allocator_to_free(self) -> float | None:
        """Per task (and again on every OOM retry): cap torch's caching
        allocator at what is physically free right now, so a pass that
        doesn't fit raises an OOM the ladder can act on instead of the
        driver quietly spilling it into system RAM — see the
        _VRAM_CAP_MARGIN notes. Best-effort; returns the fraction set."""
        import torch

        if self._device is None or self._device.type != "cuda":
            return None
        try:
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            fraction = _allocator_fraction(
                int(free_bytes), int(torch.cuda.memory_reserved()), int(total_bytes)
            )
            if fraction is not None:
                torch.cuda.set_per_process_memory_fraction(float(fraction))
            return fraction
        except Exception:  # noqa: BLE001
            return None

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
            if device.type != "cpu" and _is_device_memory_error(exc, device):
                if _is_directml_device(device):
                    request_worker_recycle(f"out of memory loading {self.model_id.value} on DirectML")
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
        """Move loaded weights to CPU after a GPU OOM. Stays there for the
        rest of this task and any later task that finds the GPU still
        tight; _ensure_model() moves it back once free VRAM clears
        _GPU_RETURN_MIN_FREE (see _return_to_gpu)."""
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

    def _return_to_gpu(
        self, descriptor: ImageModelDescriptor, device: torch.device
    ) -> ImageModelDescriptor:
        """Mirror of _relocate_to_cpu: move a descriptor an earlier task's
        OOM parked on the CPU back onto `device`, restoring bf16 where
        supported. Best-effort — a failed move leaves the descriptor on the
        CPU and this task runs there as before, never raising. Deliberately
        not routed through _notify_cpu_fallback: that hook raises the
        client's "GPU ran out of memory" dialog, and this is the recovery."""
        import torch

        free = _free_cuda_vram()
        free_label = "?" if free is None else f"{free / 1024**3:.1f}"
        print(
            f"Returning {self.model_id.value} to {device} "
            f"({free_label} GiB free after an earlier CPU fallback)…"
        )
        self._device = device
        # After a fallback the per-task cap may still sit at its floor from
        # the task that failed; recompute it before the weights move.
        self._cap_allocator_to_free()
        try:
            descriptor = descriptor.to(device).eval()
            if resolve_dtype(descriptor, device) == torch.bfloat16:
                try:
                    descriptor = descriptor.to(torch.bfloat16)
                except Exception:  # noqa: BLE001 — any bf16 hiccup means fp32
                    pass
        except Exception as exc:  # noqa: BLE001
            print(f"Could not return {self.model_id.value} to {device} ({exc}); staying on CPU…")
            _clear_device_cache(device)
            try:
                descriptor = descriptor.to(torch.device("cpu")).eval()
            except Exception:  # noqa: BLE001
                pass
            self._device = torch.device("cpu")
        return descriptor

    def _run_inference(
        self,
        descriptor: ImageModelDescriptor,
        tensor: torch.Tensor,
    ) -> torch.Tensor:
        if _is_directml_device(self._device):
            # DirectML gets its own path (see _directml_tiled_inference):
            # host-side tiling, and every pass checked before it's kept.
            return self._directml_tiled_inference(descriptor, tensor)
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
                or not _is_device_memory_error(exc, self._device)
            ):
                raise
            if _is_directml_device(self._device):
                request_worker_recycle(
                    f"out of memory on DirectML at tile {self.tile or 'off'} ({self.model_id.value})"
                )
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
            # The model load/move MUST stay inside this block. Verified on
            # torch-directml 0.2.5 (docs/directml-prelu-abort.md, Step 1
            # result): its conv2d raises "Cannot set version_counter for
            # inference tensor" whenever the parameters were moved to the
            # device outside inference mode but the activations are
            # inference tensors — which is every model, since spandrel's
            # descriptor __call__ is itself @torch.inference_mode(). Pinned
            # by tests/test_directml_prelu.py.
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
            self._cap_allocator_to_free()
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
                            self._cap_allocator_to_free()
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
            except Exception as exc:
                # Any error escaping a DirectML task leaves the plugin's
                # memory pool in whatever state the failure left it; start
                # the next task in a fresh worker (see request_worker_recycle).
                if _is_directml_device(self._device):
                    lost = _lost_directml_message(exc)
                    if lost is not None:
                        print(f"  directml error (system-language message): {lost}")
                    request_worker_recycle(f"DirectML task failed: {lost or exc}")
                raise
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

    def _directml_tiled_inference(
        self,
        descriptor: ImageModelDescriptor,
        img: torch.Tensor,
    ) -> torch.Tensor:
        """DirectML twin of _tiled_inference, with the same tile geometry,
        doing every slice and every stitch in system memory.

        Why: on an RX 9070 XT (torch-directml 0.2.5) tiles cut from a
        larger image on the device came back black or as noise — the
        left-column tiles below the first row, every model, every tile
        size — while each tile's position math is identical to the CUDA/
        MPS/CPU path that produces correct cards. DirectML mishandled
        reads/writes of sub-regions of a larger device buffer. Here the
        device only ever sees standalone contiguous tiles, copied in whole
        and copied straight back out. The untiled case goes through the
        same checked call. Returns a CPU tensor; upscale() handles that."""
        import torch

        src = img.detach().to("cpu", torch.float32)
        _, _, height, width = src.shape
        tile = self.tile if self.tile and min(height, width) > self.tile else 0
        if not tile:
            return self._checked_directml_pass(descriptor, src.contiguous(), where=(0, 0))
        scale = self.scale
        pad = self.tile_pad
        output = torch.zeros((1, 3, height * scale, width * scale), dtype=torch.float32)
        weights = torch.zeros((1, 1, height * scale, width * scale), dtype=torch.float32)
        for y in range(0, height, tile):
            for x in range(0, width, tile):
                y0, x0 = max(y - pad, 0), max(x - pad, 0)
                y1, x1 = min(y + tile + pad, height), min(x + tile + pad, width)
                tile_in = src[:, :, y0:y1, x0:x1].contiguous()
                tile_out = self._checked_directml_pass(descriptor, tile_in, where=(x0, y0))

                oy0, ox0 = (y - y0) * scale, (x - x0) * scale
                oy1 = oy0 + min(tile, height - y) * scale
                ox1 = ox0 + min(tile, width - x) * scale
                out_y0, out_x0 = y * scale, x * scale
                out_y1 = out_y0 + (oy1 - oy0)
                out_x1 = out_x0 + (ox1 - ox0)
                output[:, :, out_y0:out_y1, out_x0:out_x1] += tile_out[:, :, oy0:oy1, ox0:ox1]
                weights[:, :, out_y0:out_y1, out_x0:out_x1] += 1.0
        return output / weights.clamp_min(1.0)

    def _checked_directml_pass(
        self,
        descriptor: ImageModelDescriptor,
        tile_cpu: torch.Tensor,
        *,
        where: tuple[int, int],
    ) -> torch.Tensor:
        """One DirectML pass over a standalone CPU tile, checked before
        it's kept: non-finite or implausible output (see
        _directml_tile_problem) is retried once on the device. If the
        retry is bad too, the tile is kept exactly as it came back and a
        loud log line says so. Deliberately no CPU fallback: a visibly
        broken tile gets reported and leads to the real fault (it's how
        this very path was found), while a silent CPU detour would only
        make the app look slow. OOMs propagate untouched — the caller's
        tile ladder owns those. Returns a CPU float32 tensor."""
        import torch

        _, _, h, w = tile_cpu.shape
        for attempt in (1, 2):
            # The model's own precision on the way in (always fp32 on real
            # DirectML, but the path mustn't assume it); fp32 on the way
            # out, so the check and the stitch never see a reduced dtype.
            tile_dev = tile_cpu.to(self._device, self._dtype or torch.float32)
            out = descriptor(tile_dev).to("cpu", torch.float32)
            del tile_dev
            reason = _directml_tile_problem(out, tile_cpu, self.scale)
            if reason is None:
                if attempt == 2:
                    print(f"  directml: tile at x={where[0]} y={where[1]} ({w}x{h}) came back fine on retry")
                return out
            if attempt == 1:
                print(
                    f"  directml: tile at x={where[0]} y={where[1]} ({w}x{h}) came back "
                    f"{reason}; retrying once on the GPU"
                )
                del out
                _clear_device_cache(self._device)
        print(
            f"WARNING directml: tile at x={where[0]} y={where[1]} ({w}x{h}) came back "
            f"{reason} twice; keeping it as-is. The card will show this tile broken — "
            "please report it with this log line."
        )
        return out

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
