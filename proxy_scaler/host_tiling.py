"""Host-side tiling and output checks shared by the non-torch backends
(ncnn_backend for the ncnn models, onnx_backend for the ONNX Runtime
ones). No torch, no GPU runtime: plain numpy on float32 CHW in [0, 1].

Everything here used to live in ncnn_backend.py; it moved when a second
runtime needed the same tiling, the same "is this output an upscale of its
input at all?" gate, and the same fd-2 capture of native error lines.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence

import numpy as np

# A genuine 4x upscale box-downsampled back to the input resolution sits
# ~30 dB above it; black/garbage sits at ~10-11 dB. See _plausible.
PLAUSIBILITY_MIN_PSNR_DB = 20.0
# ...and the ceiling for "the output is just the input blown up" (see
# identity_psnr): real model outputs sit far below this.
IDENTITY_MAX_PSNR_DB = 50.0


# --- the plausibility gate --------------------------------------------------


def box_downscale(chw: np.ndarray, factor: int) -> np.ndarray:
    c, h, w = chw.shape
    h2, w2 = h // factor, w // factor
    return chw[:, : h2 * factor, : w2 * factor].reshape(c, h2, factor, w2, factor).mean(axis=(2, 4))


def consistency_psnr(output: np.ndarray, source: np.ndarray, scale: int) -> float:
    """PSNR between the output box-downscaled by `scale` and the source.
    Both float32 CHW in [0,1]; output must be exactly scale x source."""
    down = box_downscale(np.clip(output, 0.0, 1.0), scale)
    h = min(down.shape[1], source.shape[1])
    w = min(down.shape[2], source.shape[2])
    mse = float(np.mean((down[:, :h, :w] - source[:, :h, :w]) ** 2))
    if mse <= 0.0:
        return 99.0
    return float(10.0 * np.log10(1.0 / mse))


def nearest_upscale(source: np.ndarray, scale: int) -> np.ndarray:
    return np.repeat(np.repeat(source, scale, axis=1), scale, axis=2)


def identity_psnr(output: np.ndarray, source: np.ndarray, scale: int) -> float:
    """PSNR between the output and a plain nearest-neighbour blow-up of the
    source. A working model always departs from that (typically 20-35 dB
    on a card); a result that *matches* it did no work at all."""
    nn = nearest_upscale(source, scale)
    mse = float(np.mean((np.clip(output, 0.0, 1.0) - nn) ** 2))
    if mse <= 0.0:
        return 99.0
    return float(10.0 * np.log10(1.0 / mse))


def plausible(output: np.ndarray, source: np.ndarray, scale: int) -> bool:
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


# --- tiling -----------------------------------------------------------------


def _run_padded(run: Callable[[np.ndarray], np.ndarray], chunk: np.ndarray, size: int, scale: int) -> np.ndarray:
    """Run `chunk` through a model that only accepts `size` x `size` input:
    edge-pad bottom/right up to it, run, crop the output back to the
    chunk's own extent x scale. Edge padding (repeat the last row/column)
    keeps the model from seeing an artificial black border next to real
    pixels, and the padded strip is discarded anyway."""
    _, h, w = chunk.shape
    if h > size or w > size:
        raise ValueError(f"tile {w}x{h} exceeds the model's fixed input {size}x{size}")
    padded = chunk
    if h < size or w < size:
        padded = np.pad(chunk, ((0, 0), (0, size - h), (0, size - w)), mode="edge")
    out = run(np.ascontiguousarray(padded, dtype=np.float32))
    return out[:, : h * scale, : w * scale]


def run_tiled(
    run: Callable[[np.ndarray], np.ndarray],
    img: np.ndarray,
    *,
    tile: int,
    pad: int,
    scale: int,
    fixed_size: int | None = None,
) -> np.ndarray:
    """Overlapping-tile pass, same geometry as Upscaler._tiled_inference
    (pad on every side, keep the unpadded core, average overlaps).

    `run` maps float32 CHW -> float32 CHW at `scale`. `fixed_size`: the
    model only accepts N x N input (a fixed-shape ONNX export); every tile
    is then edge-padded up to N and cropped back, so `tile + 2*pad` must
    not exceed N. Without it, an image no larger than one tile goes
    through in a single untiled call."""
    _, height, width = img.shape
    if fixed_size is not None and tile + 2 * pad > fixed_size:
        raise ValueError(f"tile {tile} + 2x{pad} pad exceeds the fixed input {fixed_size}")

    def one(chunk: np.ndarray) -> np.ndarray:
        if fixed_size is None:
            return run(np.ascontiguousarray(chunk, dtype=np.float32))
        return _run_padded(run, chunk, fixed_size, scale)

    if fixed_size is None and tile >= max(height, width):
        return one(img)
    if fixed_size is not None and max(height, width) <= fixed_size:
        return one(img)
    output = np.zeros((3, height * scale, width * scale), dtype=np.float32)
    weights = np.zeros((1, height * scale, width * scale), dtype=np.float32)
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            y0, x0 = max(y - pad, 0), max(x - pad, 0)
            y1, x1 = min(y + tile + pad, height), min(x + tile + pad, width)
            tile_out = one(img[:, y0:y1, x0:x1])
            oy0, ox0 = (y - y0) * scale, (x - x0) * scale
            oy1 = oy0 + min(tile, height - y) * scale
            ox1 = ox0 + min(tile, width - x) * scale
            out_y0, out_x0 = y * scale, x * scale
            out_y1 = out_y0 + (oy1 - oy0)
            out_x1 = out_x0 + (ox1 - ox0)
            output[:, out_y0:out_y1, out_x0:out_x1] += tile_out[:, oy0:oy1, ox0:ox1]
            weights[:, out_y0:out_y1, out_x0:out_x1] += 1.0
    return output / np.maximum(weights, 1.0)


def tile_ladder(start: int, preset_tiles: Sequence[int]) -> list[int]:
    """`start`, then every preset tile below it, largest first."""
    rungs = [start] + [t for t in preset_tiles if t < start]
    return sorted(set(rungs), reverse=True)


# --- native error capture ---------------------------------------------------


class StderrCapture:
    """Temporarily divert fd 2 into a file so a native library's C/C++-side
    errors (ncnn's NCNN_LOGE, the Vulkan loader, Dawn) can be read back
    after a call. Python-level redirection can't see them; only the
    descriptor can. With echo, everything captured is re-emitted to the
    real stderr afterwards so the worker log stays complete. Best-effort:
    with no usable fd 2 (a windowless frozen process) capture is skipped
    and `text` stays empty."""

    def __init__(self, echo: bool = True) -> None:
        self.text = ""
        self._echo = echo
        self._saved: int | None = None
        self._tmp = None

    def __enter__(self) -> "StderrCapture":
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


# --- PIL <-> CHW with the card's alpha kept aside ---------------------------


def split_image(image):
    """(float32 CHW RGB in [0,1], alpha channel or None). The models are
    RGB-only; the card's real alpha (transparent rounded corners) is kept
    aside and reattached by join_image, resized to match."""
    alpha = image.getchannel("A") if image.mode in ("RGBA", "LA") else None
    rgb = image.convert("RGB")
    return np.asarray(rgb, dtype=np.float32).transpose(2, 0, 1) / 255.0, alpha


def join_image(chw: np.ndarray, alpha):
    from PIL import Image

    out8 = (np.clip(chw, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8).transpose(1, 2, 0)
    out_image = Image.fromarray(np.ascontiguousarray(out8), mode="RGB")
    if alpha is not None:
        out_image.putalpha(alpha.resize(out_image.size, Image.Resampling.LANCZOS))
    return out_image
