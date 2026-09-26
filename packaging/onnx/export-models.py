#!/usr/bin/env python3
"""Export the DAT models in models.toml to fixed-size ONNX files, one per
VRAM tier, gate each against PyTorch, measure its VRAM and speed on the
WebGPU provider, and print the registry entry for upscale.py::_WEIGHTS.

    python packaging/onnx/export-models.py export [ID ...]   # all when no ids
    make onnx-upload                                          # publish dist/onnx-models/

Why fixed-size: DAT computes its padding and attention masks from the
input size at trace time, so a traced file only runs at the size it was
exported at. The app keeps one file per tier (upscale.ONNX_TILE_PRESETS,
shapes from upscale.onnx_input_shape) and tiles every image with it.

After tracing, every GlobalAveragePool is rewritten to the equivalent
ReduceMean over H and W: ONNX Runtime's WebGPU GlobalAveragePool kernel
took 23% of a run (7.8 ms per call on a 256x256 tile), ReduceMean is
negligible. The gate below checks the rewritten file.

Needs torch + spandrel (the app venv has them), `onnx` for the exporter
(packaging/onnx/requirements-export.txt), and onnxruntime-webgpu for the
gate. Exports run on CUDA when present: the 384 px trace ran out of RAM on
the CPU. Each gate runs in a child process so its VRAM reading isn't
polluted by the exporter's own GPU memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

MANIFEST = Path(__file__).with_name("models.toml")
OUT_DIR = REPO / "dist" / "onnx-models"
SOURCE_CACHE = REPO / "tools" / "onnx-sources"
GATE_MIN_PSNR_DB = 60.0
CPU_GATE_MAX_PIXELS = 384 * 384


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_weights(model_id: str, spec: dict) -> Path:
    """The pinned upstream file: reused from weights/ when an identical
    copy is already there, else downloaded into tools/onnx-sources/."""
    import requests

    name = Path(spec["source_url"]).name
    for candidate in (REPO / "weights" / name, SOURCE_CACHE / name):
        if candidate.exists() and sha256_of(candidate) == spec["source_sha256"]:
            return candidate
    dest = SOURCE_CACHE / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetching {spec['source_url']}")
    with requests.get(spec["source_url"], stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fp:
            for chunk in resp.iter_content(1 << 20):
                fp.write(chunk)
    got = sha256_of(dest)
    if got != spec["source_sha256"]:
        raise SystemExit(f"{model_id}: source sha256 {got} != pinned {spec['source_sha256']}")
    return dest


def reference_crop(h: int, w: int):
    """A real card crop of h x w (float32 CHW) from the image cache."""
    import numpy as np
    from PIL import Image

    cards = [p for p in sorted((REPO / "imgcache" / "originals").glob("*.png")) if "thumb" not in p.name]
    if not cards:
        raise SystemExit("no card in imgcache/originals to gate against — generate or fetch one first")
    img = np.asarray(Image.open(cards[0]).convert("RGB"), dtype=np.float32).transpose(2, 0, 1) / 255.0
    _, ih, iw = img.shape
    y0, x0 = max(0, (ih - h) // 3), max(0, (iw - w) // 3)
    return np.ascontiguousarray(img[:, y0 : y0 + h, x0 : x0 + w])


def rewrite_global_average_pool(path: Path) -> int:
    """GlobalAveragePool -> ReduceMean(axes=[2, 3], keepdims=1), in place.
    Same math; see the module docstring for why."""
    import onnx
    from onnx import helper

    model = onnx.load(str(path))
    graph = model.graph
    count = 0
    for i, node in enumerate(list(graph.node)):
        if node.op_type != "GlobalAveragePool":
            continue
        graph.node.remove(node)
        graph.node.insert(
            i,
            helper.make_node(
                "ReduceMean", [node.input[0]], [node.output[0]],
                name=node.name + "_reducemean", axes=[2, 3], keepdims=1,
            ),
        )
        count += 1
    onnx.checker.check_model(model)
    onnx.save(model, str(path))
    return count


def export_model(model_id: str, spec: dict) -> dict:
    import numpy as np
    import torch
    from spandrel import ModelLoader

    from proxy_scaler.upscale import ONNX_TILE_PRESETS, UpscaleModel, onnx_filename, onnx_input_shape

    model = UpscaleModel(model_id)
    source = source_weights(model_id, spec)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    module = ModelLoader().load_from_file(str(source)).eval().model.to(device)
    for p in module.parameters():
        p.requires_grad_(False)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    for preset in ONNX_TILE_PRESETS:
        shape = onnx_input_shape(preset.tile)
        path = OUT_DIR / onnx_filename(model, shape)
        crop = reference_crop(*shape)
        started = time.perf_counter()
        with torch.no_grad():
            torch.onnx.export(
                module,
                torch.from_numpy(crop)[None].to(device),
                str(path),
                dynamo=False,
                opset_version=17,
                input_names=["data"],
                output_names=["output"],
                do_constant_folding=False,
            )
            ref = module(torch.from_numpy(crop)[None].to(device))[0].clamp(0, 1).float().cpu().numpy()
        np.save(OUT_DIR / f".{path.stem}.ref.npy", ref)
        np.save(OUT_DIR / f".{path.stem}.in.npy", crop)
        pools = rewrite_global_average_pool(path)
        print(
            f"  exported {path.name} ({path.stat().st_size / 1e6:.1f} MB, {pools} pools "
            f"rewritten) in {time.perf_counter() - started:.0f}s"
        )
        results[path.name] = path
    del module
    if device == "cuda":
        torch.cuda.empty_cache()
    entry = {}
    for name, path in results.items():
        gate = json.loads(
            subprocess.run(
                [sys.executable, __file__, "_gate", str(path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip().splitlines()[-1]
        )
        print(f"  gate {path.name}: {gate}")
        cpu_ok = gate["cpu_psnr"] is None or gate["cpu_psnr"] >= GATE_MIN_PSNR_DB
        if not cpu_ok or gate["webgpu_psnr"] < GATE_MIN_PSNR_DB:
            raise SystemExit(f"{path.name}: not faithful to the PyTorch model ({gate})")
        entry[name] = {"file": path.name, "sha256": sha256_of(path), "size": path.stat().st_size, "gate": gate}
    return entry


def gate(path: Path) -> None:
    """Child process: ORT CPU and WebGPU vs the saved PyTorch reference,
    plus WebGPU VRAM (nvidia-smi, when present) and ms per tile."""
    import numpy as np
    import onnxruntime as ort

    ref = np.load(path.parent / f".{path.stem}.ref.npy")
    crop = np.load(path.parent / f".{path.stem}.in.npy")[None]

    def psnr(a):
        mse = float(np.mean((np.clip(a[0], 0, 1) - ref) ** 2))
        return 99.0 if mse == 0 else round(10 * np.log10(1 / mse), 1)

    def vram_mb():
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=True,
            ).stdout
            return int(out.split()[0])
        except Exception:  # noqa: BLE001
            return None

    # The CPU cross-check proves the export itself; above 384x384 px DAT's
    # attention on the CPU needs more RAM than a build box has (a 512x512
    # run was OOM-killed), so larger files are gated on WebGPU alone.
    cpu_psnr = None
    if crop.shape[-1] * crop.shape[-2] <= CPU_GATE_MAX_PIXELS:
        so = ort.SessionOptions()
        so.log_severity_level = 3
        cpu = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        cpu_psnr = psnr(cpu.run(["output"], {"data": crop})[0])
        del cpu
    # Peak, sampled while the session runs: memory read after a run is
    # about half the transient peak and badly understates what a tier needs.
    import threading

    before = vram_mb()
    peak = [0]
    done = threading.Event()

    def sample() -> None:
        while not done.is_set():
            now = vram_mb()
            if now is not None and before is not None:
                peak[0] = max(peak[0], now - before)
            time.sleep(0.05)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    so2 = ort.SessionOptions()
    so2.log_severity_level = 3
    so2.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    so2.add_session_config_entry("ep.webgpuexecutionprovider.storageBufferCacheMode", "simple")
    gpu = ort.InferenceSession(str(path), so2, providers=["WebGpuExecutionProvider"])
    out = gpu.run(["output"], {"data": crop})[0]
    started = time.perf_counter()
    for _ in range(3):
        gpu.run(["output"], {"data": crop})
    ms = round((time.perf_counter() - started) / 3 * 1000)
    time.sleep(0.2)
    done.set()
    sampler.join()
    print(json.dumps({
        "cpu_psnr": cpu_psnr,
        "webgpu_psnr": psnr(out),
        "webgpu_ms_per_tile": ms,
        "webgpu_peak_vram_mb": peak[0] if before is not None else None,
    }))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("export", "_gate"))
    ap.add_argument("args", nargs="*")
    ns = ap.parse_args()
    if ns.command == "_gate":
        gate(Path(ns.args[0]))
        return 0
    with MANIFEST.open("rb") as fp:
        manifest = tomllib.load(fp)
    ids = ns.args or list(manifest)
    out = OUT_DIR / "manifest.json"
    results = json.loads(out.read_text()) if out.exists() else {}
    for model_id in ids:
        print(f"== {model_id}")
        entry = export_model(model_id, manifest[model_id])
        results[model_id] = entry
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2) + "\n")
        enum = model_id.upper()
        shas = ", ".join(f'"{k}": "{v["sha256"]}"' for k, v in entry.items())
        print(f"  registry entry:\n    (UpscaleModel.{enum}, 4): _onnx_tiers(UpscaleModel.{enum}, {{{shas}}}),")
    for f in OUT_DIR.glob(".*.npy"):
        f.unlink()
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
