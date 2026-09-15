"""Pin down the torch-directml op that aborts the worker on the
realesrgan_anime_fast model — run this on the DirectML box, never in the
worker.

Each candidate runs in its OWN subprocess: the failure being probed is a
native CHECK that kills the process (exit code 127 / "Check failed: rank
<= DML_TENSOR_DIMENSION_COUNT_MAX" from dml_tensor_desc.cc), which no
try/except can catch. The parent only reports per-probe exit codes.

Expected on torch 2.4.1 + torch-directml 0.2.5 (see the accompanying
docs/directml-prelu-abort.md):

    prelu_channelwise      ABORT   <- the culprit
    prelu_single           ok
    prelu_safe_channelwise ok      <- the workaround upscale.py applies
    pixel_shuffle          ok
    interpolate_nearest    ok
    compact_native         ABORT   (full anime-fast model, unpatched)
    compact_patched        ok      (full anime-fast model, as the worker loads it)

Usage (from the repo root, in the directml venv):

    python docs/scripts/directml_prelu_probe.py            # all probes
    python docs/scripts/directml_prelu_probe.py prelu_channelwise compact_patched
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEIGHTS = ROOT / "weights" / "realesr-animevideov3.pth"

PROBES: dict[str, str] = {
    "prelu_channelwise": """
import torch, torch_directml
dev = torch_directml.device()
m = torch.nn.PReLU(num_parameters=64).to(dev)
y = m(torch.rand(1, 64, 32, 32, device=dev)); print(y.sum().item())
""",
    "prelu_single": """
import torch, torch_directml
dev = torch_directml.device()
m = torch.nn.PReLU().to(dev)
y = m(torch.rand(1, 64, 32, 32, device=dev)); print(y.sum().item())
""",
    "prelu_safe_channelwise": """
import torch, torch_directml
from proxy_scaler.upscale import _make_directml_safe_prelu
dev = torch_directml.device()
orig = torch.nn.PReLU(num_parameters=64)
with torch.no_grad(): orig.weight.copy_(torch.rand(64) * 2 - 1)
x = torch.rand(1, 64, 32, 32) * 2 - 1
ref = orig(x)
safe = _make_directml_safe_prelu(orig).to(dev)
y = safe(x.to(dev)).cpu()
print("max abs diff vs CPU nn.PReLU:", (y - ref).abs().max().item())
""",
    "pixel_shuffle": """
import torch, torch_directml
dev = torch_directml.device()
y = torch.nn.PixelShuffle(4)(torch.rand(1, 48, 32, 32, device=dev)); print(y.shape)
""",
    "interpolate_nearest": """
import torch, torch_directml, torch.nn.functional as F
dev = torch_directml.device()
y = F.interpolate(torch.rand(1, 3, 32, 32, device=dev), scale_factor=4, mode="nearest"); print(y.shape)
""",
    "compact_native": f"""
import torch, torch_directml
from spandrel import ModelLoader
dev = torch_directml.device()
d = ModelLoader().load_from_file(r"{WEIGHTS}").to(dev).eval()
with torch.inference_mode(): y = d(torch.rand(1, 3, 64, 64, device=dev))
print(y.shape)
""",
    "compact_patched": f"""
import torch, torch_directml
from spandrel import ModelLoader
from proxy_scaler.upscale import replace_channelwise_prelu
dev = torch_directml.device()
d = ModelLoader().load_from_file(r"{WEIGHTS}")
ref_model = d.model.eval()
x = torch.rand(1, 3, 64, 64)
with torch.inference_mode(): ref = ref_model(x)
print("swapped", replace_channelwise_prelu(d.model), "PReLU layers")
d = d.to(dev).eval()
with torch.inference_mode(): y = d(x.to(dev)).cpu()
print("max abs diff vs CPU native:", (y - ref).abs().max().item())
""",
}


def main(argv: list[str]) -> int:
    try:
        import torch_directml  # noqa: F401
    except ImportError:
        print(
            "torch_directml is not installed in this interpreter — this probe "
            "only means anything in the directml venv on a Windows box "
            "(PY=\"py -3.12\" GPU_VARIANT=directml make reinstall).",
            file=sys.stderr,
        )
        return 2
    if not WEIGHTS.is_file():
        print(f"missing {WEIGHTS} — run any anime-fast generation once to download it", file=sys.stderr)
        return 2
    names = argv or list(PROBES)
    unknown = [n for n in names if n not in PROBES]
    if unknown:
        print(f"unknown probe(s): {unknown}; choose from {list(PROBES)}", file=sys.stderr)
        return 2
    worst = 0
    for name in names:
        proc = subprocess.run(
            [sys.executable, "-c", PROBES[name]],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        verdict = "ok" if proc.returncode == 0 else f"ABORT (exit {proc.returncode})"
        print(f"{name:24s} {verdict}")
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        for line in tail:
            print(f"    {line}")
        worst = max(worst, proc.returncode != 0)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
