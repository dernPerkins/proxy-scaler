#!/usr/bin/env python3
"""Turn the source models in models.toml into the ncnn .param/.bin pairs
the app downloads for its "Vulkan Models", gate each one against the
PyTorch original, and print the registry entry to paste into
upscale.py::_WEIGHTS.

    python packaging/ncnn/convert-models.py convert [ID ...]   # all when no ids
    python packaging/ncnn/convert-models.py pin ID ...         # fill source_sha256
    make ncnn-upload                                           # publish dist/ncnn-models/

Run it from the conversion venv (packaging/ncnn/requirements-convert.txt),
never the app venv. Outputs land in dist/ncnn-models/<id>.param|.bin plus
manifest.json (sha256 + size per file); sources are cached in
tools/ncnn-sources/ (gitignored).

Two routes per model:
- official_ncnn: the author already ships ncnn files -> fetched as-is (a
  release zip member or direct URLs), only the blob names are normalised.
- converter = "esrgan": the direct RRDBNet emitter (esrgan2ncnn.py) —
  pnnx can't fit a 23-block RRDBNet in this box's RAM.
- otherwise: spandrel loads the PyTorch weights, pnnx traces the module at
  two input sizes (so height/width stay dynamic) and emits fp16 ncnn files.

Either way the pair then runs through the app's own ncnn backend on a
real card and must match the PyTorch output (PSNR >= GATE_MIN_PSNR_DB) or
the model is rejected. Blob names are forced to `data` / `output`, which
is what proxy_scaler/ncnn_backend.py feeds and reads.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import numpy as np
import requests
from PIL import Image

try:
    import tomllib  # 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from proxy_scaler.ncnn_backend import INPUT_BLOB, OUTPUT_BLOB, _NcnnNet, select_vulkan_device  # noqa: E402

MANIFEST = Path(__file__).with_name("models.toml")
OUT_DIR = REPO / "dist" / "ncnn-models"
SOURCE_CACHE = REPO / "tools" / "ncnn-sources"
SCALE = 4
GATE_MIN_PSNR_DB = 35.0
GATE_TILE = 256
GATE_PAD = 32
GATE_CROP = 384
# pnnx traces at these two sizes; differing sizes are what mark H/W dynamic.
TRACE_SHAPES = ((1, 3, 64, 64), (1, 3, 96, 80))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetching {url}")
    with requests.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fp:
            for chunk in resp.iter_content(1 << 20):
                fp.write(chunk)
    tmp.replace(dest)
    return dest


def normalise_param(text: str) -> str:
    """Force the single Input blob to `data` and the last layer's output
    blob to `output` (pnnx emits in0/out0; official files vary)."""
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "7767517":
        raise SystemExit("not an ncnn param file (bad magic)")
    header = lines[:2]
    body = [line for line in lines[2:] if line.strip()]
    inputs = [i for i, line in enumerate(body) if line.split()[0] == "Input"]
    if len(inputs) != 1:
        raise SystemExit(f"expected exactly one Input layer, found {len(inputs)}")
    in_tokens = body[inputs[0]].split()
    old_in = in_tokens[4]
    # Output blob: the last layer's last blob token before its params.
    last = body[-1].split()
    n_in, n_out = int(last[2]), int(last[3])
    old_out = last[4 + n_in + n_out - 1]
    if old_in == INPUT_BLOB and old_out == OUTPUT_BLOB:
        # Already the right names (the official Real-ESRGAN files): keep
        # the bytes exactly, so the published hash is the author's file.
        return text if text.endswith("\n") else text + "\n"
    renamed = []
    for line in body:
        tokens = line.split()
        n_in, n_out = int(tokens[2]), int(tokens[3])
        blobs = tokens[4 : 4 + n_in + n_out]
        blobs = [INPUT_BLOB if b == old_in else OUTPUT_BLOB if b == old_out else b for b in blobs]
        renamed.append(" ".join(tokens[:4] + blobs + tokens[4 + n_in + n_out :]))
    return "\n".join(header + renamed) + "\n"


def load_manifest() -> dict:
    with MANIFEST.open("rb") as fp:
        return tomllib.load(fp)


def official_pair(model_id: str, spec: dict) -> tuple[bytes, bytes]:
    off = spec["official_ncnn"]
    if "archive" in off:
        archive = fetch(off["archive"], SOURCE_CACHE / Path(off["archive"]).name)
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()

            def member(suffix: str) -> bytes:
                match = [n for n in names if n.endswith(suffix)]
                if len(match) != 1:
                    raise SystemExit(f"{model_id}: {suffix} matches {match} in {archive.name}")
                return zf.read(match[0])

            return member(off["param"]), member(off["bin"])
    param = fetch(off["param"], SOURCE_CACHE / f"{model_id}-official.param")
    weights = fetch(off["bin"], SOURCE_CACHE / f"{model_id}-official.bin")
    return param.read_bytes(), weights.read_bytes()


def source_weights(model_id: str, spec: dict, *, pin: bool) -> Path:
    url = spec["source_url"]
    dest = SOURCE_CACHE / f"{model_id}{Path(url).suffix}"
    fetch(url, dest)
    digest = sha256_of(dest)
    if pin:
        print(f"{model_id}: source_sha256 = \"{digest}\"")
        return dest
    expected = spec.get("source_sha256") or ""
    if not expected:
        raise SystemExit(f"{model_id}: models.toml has no source_sha256 — run `convert-models.py pin {model_id}` and paste it")
    if digest != expected:
        raise SystemExit(f"{model_id}: source sha256 {digest} != pinned {expected}")
    return dest


def convert_with_pnnx(model_id: str, weights: Path, workdir: Path) -> tuple[bytes, bytes]:
    import pnnx
    import torch
    from spandrel import ModelLoader

    descriptor = ModelLoader().load_from_file(str(weights)).eval()
    module = descriptor.model.eval()
    workdir.mkdir(parents=True, exist_ok=True)
    pt = workdir / f"{model_id}.pt"
    cwd = os.getcwd()
    os.chdir(workdir)  # pnnx writes its outputs next to the .pt
    try:
        pnnx.export(
            module,
            str(pt),
            inputs=torch.rand(*TRACE_SHAPES[0]),
            inputs2=torch.rand(*TRACE_SHAPES[1]),
            fp16=True,
            optlevel=2,
        )
    finally:
        os.chdir(cwd)
    param = workdir / f"{model_id}.ncnn.param"
    weights_out = workdir / f"{model_id}.ncnn.bin"
    if not param.exists() or not weights_out.exists():
        raise SystemExit(f"{model_id}: pnnx produced no ncnn files (see its output above)")
    return param.read_bytes(), weights_out.read_bytes()


def convert_esrgan(model_id: str, weights: Path) -> tuple[bytes, bytes]:
    """RRDBNet via the direct emitter (esrgan2ncnn.py): pnnx needs >15 GB
    for a 23-block RRDBNet and this layout is exactly Real-ESRGAN's own."""
    from spandrel import ModelLoader

    sys.path.insert(0, str(Path(__file__).parent))
    from esrgan2ncnn import esrgan_to_ncnn  # noqa: E402

    descriptor = ModelLoader().load_from_file(str(weights)).eval()
    param, weights_bytes = esrgan_to_ncnn(descriptor.model, input_blob=INPUT_BLOB, output_blob=OUTPUT_BLOB)
    return param.encode("utf-8"), weights_bytes


def torch_reference(weights: Path, chw: np.ndarray) -> np.ndarray:
    import torch
    from spandrel import ModelLoader

    descriptor = ModelLoader().load_from_file(str(weights)).eval()
    # CUDA when present purely for speed (an RRDBNet on a 745x1040 card is
    # minutes on a CPU); fp32 either way so the reference is the model's
    # own answer, not a precision compromise.
    device = "cuda" if torch.cuda.is_available() else "cpu"
    descriptor = descriptor.to(device)
    with torch.no_grad():
        out = descriptor.model(torch.from_numpy(np.ascontiguousarray(chw))[None].to(device))[0]
    return out.clamp(0, 1).float().cpu().numpy()


def tiled(net: _NcnnNet, img: np.ndarray, tile: int, pad: int) -> np.ndarray:
    _, h, w = img.shape
    out = np.zeros((3, h * SCALE, w * SCALE), np.float32)
    wgt = np.zeros((1, h * SCALE, w * SCALE), np.float32)
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            y0, x0 = max(y - pad, 0), max(x - pad, 0)
            y1, x1 = min(y + tile + pad, h), min(x + tile + pad, w)
            o = net.run(img[:, y0:y1, x0:x1])
            oy0, ox0 = (y - y0) * SCALE, (x - x0) * SCALE
            oy1, ox1 = oy0 + min(tile, h - y) * SCALE, ox0 + min(tile, w - x) * SCALE
            out[:, y * SCALE : y * SCALE + (oy1 - oy0), x * SCALE : x * SCALE + (ox1 - ox0)] += o[:, oy0:oy1, ox0:ox1]
            wgt[:, y * SCALE : y * SCALE + (oy1 - oy0), x * SCALE : x * SCALE + (ox1 - ox0)] += 1
    return out / np.maximum(wgt, 1)


def gate(model_id: str, param: Path, weights: Path, source: Path | None, card: Path) -> dict:
    img = Image.open(card).convert("RGB")
    chw = np.asarray(img, np.float32).transpose(2, 0, 1) / 255.0
    gpu = select_vulkan_device()
    net = _NcnnNet(param, weights, gpu, fp16=gpu is not None)
    out = tiled(net, chw, GATE_TILE, GATE_PAD)
    result = {
        "device": "cpu" if gpu is None else f"vulkan:{gpu}",
        "output_shape": list(out.shape),
        "finite": bool(np.isfinite(out).all()),
        "mean": float(np.clip(out, 0, 1).mean()),
    }
    if out.shape != (3, chw.shape[1] * SCALE, chw.shape[2] * SCALE):
        raise SystemExit(f"{model_id}: output shape {out.shape} is not {SCALE}x the input")
    if not result["finite"] or result["mean"] < 0.02:
        raise SystemExit(f"{model_id}: output is NaN/black ({result})")
    if source is not None:
        # Faithfulness is judged on one untiled crop: the same pixels go
        # through PyTorch and ncnn with no tiling on either side, and an
        # RRDBNet on a 384px crop fits any GPU (a full card in fp32 needs
        # ~6 GB and OOMs a shared 12 GB card).
        crop = np.ascontiguousarray(chw[:, :GATE_CROP, :GATE_CROP])
        ref = torch_reference(source, crop)
        ncnn_crop = np.clip(net.run(crop), 0, 1)
        mse = float(np.mean((ncnn_crop - ref) ** 2))
        result["psnr_vs_torch_db"] = 99.0 if mse == 0 else float(10 * np.log10(1 / mse))
        if result["psnr_vs_torch_db"] < GATE_MIN_PSNR_DB:
            raise SystemExit(
                f"{model_id}: ncnn output is {result['psnr_vs_torch_db']:.1f} dB from the "
                f"PyTorch original (gate {GATE_MIN_PSNR_DB}); conversion is not faithful"
            )
    return result


def convert(model_id: str, spec: dict, card: Path, skip_gate: bool) -> dict:
    print(f"== {model_id} ({spec['arch']}, {spec['license']})")
    source: Path | None = None
    if "official_ncnn" in spec:
        param_bytes, bin_bytes = official_pair(model_id, spec)
        if spec.get("source_sha256"):
            source = source_weights(model_id, spec, pin=False)
    elif spec.get("converter") == "esrgan":
        source = source_weights(model_id, spec, pin=False)
        param_bytes, bin_bytes = convert_esrgan(model_id, source)
    else:
        source = source_weights(model_id, spec, pin=False)
        param_bytes, bin_bytes = convert_with_pnnx(model_id, source, SOURCE_CACHE / "pnnx" / model_id)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    param = OUT_DIR / f"{model_id}.param"
    weights = OUT_DIR / f"{model_id}.bin"
    param.write_text(normalise_param(param_bytes.decode("utf-8")))
    weights.write_bytes(bin_bytes)
    entry = {
        "param": {"sha256": sha256_of(param), "size": param.stat().st_size},
        "bin": {"sha256": sha256_of(weights), "size": weights.stat().st_size},
        "arch": spec["arch"],
        "license": spec["license"],
        "author": spec["author"],
        "homepage": spec["homepage"],
    }
    if not skip_gate:
        entry["gate"] = gate(model_id, param, weights, source, card)
        print(f"  gate: {entry['gate']}")
    print(
        "  registry entry:\n"
        f"    (UpscaleModel.{model_id.upper()}, 4): _ncnn_pair(\n"
        f"        UpscaleModel.{model_id.upper()},\n"
        f"        \"{entry['param']['sha256']}\",\n"
        f"        \"{entry['bin']['sha256']}\",\n"
        "    ),"
    )
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("convert", "pin"))
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--card", type=Path, default=None, help="reference card PNG for the gate (default: first file in imgcache/originals)")
    ap.add_argument("--skip-gate", action="store_true")
    args = ap.parse_args()
    manifest = load_manifest()
    ids = args.ids or list(manifest)
    unknown = [i for i in ids if i not in manifest]
    if unknown:
        raise SystemExit(f"not in models.toml: {unknown}")
    if args.command == "pin":
        for model_id in ids:
            source_weights(model_id, manifest[model_id], pin=True)
        return 0
    card = args.card or next(iter(sorted((REPO / "imgcache" / "originals").glob("*.png"))), None)
    if card is None and not args.skip_gate:
        raise SystemExit("no reference card: pass --card <745x1040 png> or generate one first")
    results_path = OUT_DIR / "manifest.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else {}
    for model_id in ids:
        results[model_id] = convert(model_id, manifest[model_id], card, args.skip_gate)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nwrote {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
