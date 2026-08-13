"""Multi-model upscaling (Real-ESRGAN, RealESRNet, SwinIR) via Spandrel."""

from __future__ import annotations

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
    REALESRGAN = "realesrgan"
    REALESRNET = "realesrnet"
    SWINIR = "swinir"
    REALESRGAN_ANIME = "realesrgan_anime"
    REALESRGAN_ANIME_FAST = "realesrgan_anime_fast"
    ILLUSTRATIONJANAI = "illustrationjanai"
    ULTRASHARP = "ultrasharp"
    ULTRASHARP_V2 = "ultrasharp_v2"
    HAT = "hat"

    @property
    def label(self) -> str:
        return {
            UpscaleModel.REALESRGAN: "Real-ESRGAN (fast, sharper / more invented detail)",
            UpscaleModel.REALESRNET: "RealESRNet (less hallucination, better for text)",
            UpscaleModel.SWINIR: "SwinIR classical (fidelity-first, slower)",
            UpscaleModel.REALESRGAN_ANIME: (
                "Real-ESRGAN Anime (official, tuned for illustrated/non-photo art)"
            ),
            UpscaleModel.REALESRGAN_ANIME_FAST: (
                "Real-ESRGAN Anime Fast (compact/lightweight, tuned for anime video)"
            ),
            UpscaleModel.ILLUSTRATIONJANAI: (
                "IllustrationJaNai (trained on digital art/illustrations, not photos)"
            ),
            UpscaleModel.ULTRASHARP: (
                "UltraSharp (original, predecessor to UltraSharpV2)"
            ),
            UpscaleModel.ULTRASHARP_V2: (
                "UltraSharpV2 (general-purpose, strong on illustration/artwork)"
            ),
            UpscaleModel.HAT: "HAT (newer transformer architecture, high fidelity)",
        }[self]

    @property
    def supported_scales(self) -> tuple[int, ...]:
        if self in (
            UpscaleModel.REALESRNET,
            UpscaleModel.REALESRGAN_ANIME,
            UpscaleModel.REALESRGAN_ANIME_FAST,
            UpscaleModel.ILLUSTRATIONJANAI,
            UpscaleModel.ULTRASHARP,
            UpscaleModel.ULTRASHARP_V2,
            UpscaleModel.HAT,
        ):
            return (4,)
        return (2, 4)


# Transformer/attention-heavy architectures that can OOM a ~12GB GPU on a
# full-image forward pass — the lighter CNN-based models don't need tiling.
# Ported from the old Streamlit UI (ui/decklist.py::_effective_tile_size)
# when that module was deleted — real behavior, not UI-specific, so it
# belongs here next to UpscaleModel rather than disappearing with the UI.
HEAVY_MODELS = frozenset(
    {
        UpscaleModel.ILLUSTRATIONJANAI,
        UpscaleModel.ULTRASHARP,
        UpscaleModel.ULTRASHARP_V2,
        UpscaleModel.HAT,
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
    (UpscaleModel.REALESRGAN, 2): _WeightSpec(
        "RealESRGAN_x2plus.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
    ),
    (UpscaleModel.REALESRGAN, 4): _WeightSpec(
        "RealESRGAN_x4plus.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
    ),
    (UpscaleModel.REALESRNET, 4): _WeightSpec(
        "RealESRNet_x4plus.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.1/RealESRNet_x4plus.pth",
    ),
    (UpscaleModel.SWINIR, 2): _WeightSpec(
        "001_classicalSR_DF2K_s64w8_SwinIR-M_x2.pth",
        "https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/001_classicalSR_DF2K_s64w8_SwinIR-M_x2.pth",
    ),
    (UpscaleModel.SWINIR, 4): _WeightSpec(
        "001_classicalSR_DF2K_s64w8_SwinIR-M_x4.pth",
        "https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/001_classicalSR_DF2K_s64w8_SwinIR-M_x4.pth",
    ),
    (UpscaleModel.REALESRGAN_ANIME, 4): _WeightSpec(
        "RealESRGAN_x4plus_anime_6B.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
    ),
    # Official release — the "Compact" (SRVGGNetCompact) architecture,
    # much smaller/faster than the RRDBNet-based anime model above.
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
    # Original 4x-UltraSharp (Kim2091), predecessor to UltraSharpV2 above.
    # Official host is Mega.nz only, which our simple streaming downloader
    # can't handle (same limitation as IllustrationJaNai/HAT below) — using
    # a verified third-party HuggingFace mirror instead. If this mirror
    # disappears, search for "4x-UltraSharp.pth" on huggingface.co for a
    # replacement (multiple community mirrors exist).
    (UpscaleModel.ULTRASHARP, 4): _WeightSpec(
        "4x-UltraSharp.pth",
        "https://huggingface.co/lowlione/4x-UltraSharp.pth/resolve/main/4x-UltraSharp.pth",
    ),
    # CC-BY-NC-SA-4.0 (non-commercial). Officially hosted by the creator.
    (UpscaleModel.ULTRASHARP_V2, 4): _WeightSpec(
        "4x-UltraSharpV2.safetensors",
        "https://huggingface.co/Kim2091/UltraSharpV2/resolve/main/4x-UltraSharpV2.safetensors",
    ),
    # Official author hosts on Google Drive only (same limitation as
    # IllustrationJaNai above) — using a third-party HuggingFace mirror.
    # If this mirror disappears, search for "Real_HAT_GAN_SRx4" on
    # huggingface.co for a replacement.
    (UpscaleModel.HAT, 4): _WeightSpec(
        "Real_HAT_GAN_SRx4.pth",
        "https://huggingface.co/jaideepsingh/upscale_models/resolve/main/HAT/Real_HAT_GAN_SRx4.pth",
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


@dataclass(frozen=True)
class UpscaleResult:
    """Upscaled image plus where inference ran."""

    image: Image.Image
    device: str  # "gpu" | "cpu"


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


class Upscaler:
    """Lazy-loaded Spandrel upscaler for a chosen model + scale."""

    def __init__(
        self,
        model: UpscaleModel | str = UpscaleModel.REALESRGAN,
        scale: int = 4,
        weights_dir: Path | str = "weights",
        tile: int = 0,
        tile_pad: int = 32,
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
        self._descriptor: ImageModelDescriptor | None = None
        self._device: torch.device | None = None

    def _ensure_model(self) -> ImageModelDescriptor:
        if self._descriptor is not None:
            return self._descriptor

        import torch
        from spandrel import ImageModelDescriptor, ModelLoader

        device = resolve_device()
        self._device = device
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
        try:
            descriptor = descriptor.to(device).eval()
        except Exception as exc:
            if device.type != "cpu" and _is_oom_error(exc):
                print(
                    f"OOM loading model on {device}; clearing cache and falling back to CPU…"
                )
                _clear_device_cache(device)
                device = torch.device("cpu")
                self._device = device
                descriptor = descriptor.to(device).eval()
            else:
                raise
        self._descriptor = descriptor
        return descriptor

    def _relocate_to_cpu(self) -> ImageModelDescriptor:
        """Move loaded weights to CPU after a GPU OOM (stays on CPU afterward)."""
        import torch

        assert self._descriptor is not None
        old = self._device
        _clear_device_cache(old)
        print(f"Falling back to CPU upscale (was {old})…")
        self._device = torch.device("cpu")
        self._descriptor = self._descriptor.to(self._device).eval()
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
            tensor = to_tensor(rgb).unsqueeze(0).to(self._device)

            try:
                try:
                    out_gpu = self._run_inference(descriptor, tensor)
                except Exception as exc:
                    if self._device.type == "cpu" or not _is_oom_error(exc):
                        raise
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
                    tensor = to_tensor(rgb).unsqueeze(0).to(self._device)
                    out_gpu = self._run_inference(descriptor, tensor)

                out_cpu = out_gpu.clamp(0.0, 1.0).squeeze(0).cpu()
                del out_gpu
                out_image = to_pil_image(out_cpu)
                if alpha is not None:
                    resized_alpha = alpha.resize(out_image.size, Image.Resampling.LANCZOS)
                    out_image.putalpha(resized_alpha)
                return UpscaleResult(
                    image=out_image,
                    device=device_kind(self._device),
                )
            finally:
                try:
                    del tensor
                except NameError:
                    pass
                _clear_device_cache(self._device)

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
) -> UpscaleResult:
    """Return upscaled image (+ device), using disk cache when present."""
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
    result = upscaler.upscale(src)
    result.image.save(path, format="PNG")
    write_cache_device(path, result.device)
    return result
