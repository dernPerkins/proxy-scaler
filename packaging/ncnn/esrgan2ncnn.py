"""Direct RRDBNet (ESRGAN / Real-ESRGAN) -> ncnn emitter.

pnnx traces a 23-block RRDBNet into a graph large enough that its ncnn
pass needs >15 GB of RAM, and its optlevel=0 path aborts on the same
graph. The architecture itself is tiny to describe, so this writes the
.param/.bin pair straight from the module: the exact layout of Real-
ESRGAN's own ncnn release (realesrgan-x4plus-anime.param — Split fan-outs,
Concat order, Eltwise residuals with (0.2, 1.0) coefficients, BinaryOp
trunk skip, nearest Interp x2 twice), so the files are interchangeable
with the official ones. Validated by emitting RealESRGAN_x4plus_anime_6B
and comparing with the official conversion (see convert-models.py
`selftest-esrgan`).

Bin format: per Convolution, in param order, a 4-byte weight-type flag
(0x01306B47 = fp16), the fp16 weights in PyTorch's [out][in][kh][kw]
order, then the fp32 bias with no flag — ncnn's ModelBin contract.
"""

from __future__ import annotations

import struct

import numpy as np
import torch

FP16_FLAG = struct.pack("<I", 0x01306B47)
LRELU_SLOPE = 0.2
RESIDUAL_SCALE = 0.2


class _Emitter:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.blobs = 0
        self.weights = bytearray()
        self._names: dict[str, int] = {}

    def _name(self, kind: str) -> str:
        n = self._names.get(kind, 0)
        self._names[kind] = n + 1
        return f"{kind}_{n}"

    def _blob(self) -> str:
        self.blobs += 1
        return f"b{self.blobs}"

    def input(self, name: str) -> str:
        self.lines.append(f"Input {self._name('input')} 0 1 {name}")
        self.blobs += 1
        return name

    def conv(self, x: str, weight: np.ndarray, bias: np.ndarray, *, lrelu: bool, out_name: str | None = None) -> str:
        out_ch, in_ch, kh, kw = weight.shape
        assert kh == kw == 3, "RRDBNet convs are 3x3"
        y = out_name or self._blob()
        if out_name:
            self.blobs += 1
        params = f"0={out_ch} 1=3 4=1 5=1 6={weight.size}"
        if lrelu:
            params += f" 9=2 -23310=1,{LRELU_SLOPE:e}"
        self.lines.append(f"Convolution {self._name('conv')} 1 1 {x} {y} {params}")
        self.weights += FP16_FLAG
        self.weights += np.ascontiguousarray(weight, dtype=np.float32).astype(np.float16).tobytes()
        self.weights += np.ascontiguousarray(bias, dtype=np.float32).tobytes()
        return y

    def split(self, x: str, n: int) -> list[str]:
        if n == 1:
            return [x]
        outs = [self._blob() for _ in range(n)]
        self.lines.append(f"Split {self._name('split')} 1 {n} {x} {' '.join(outs)}")
        return outs

    def concat(self, xs: list[str]) -> str:
        y = self._blob()
        self.lines.append(f"Concat {self._name('concat')} {len(xs)} 1 {' '.join(xs)} {y}")
        return y

    def residual(self, branch: str, skip: str) -> str:
        """skip + RESIDUAL_SCALE * branch, as ncnn's Eltwise SUM with coefficients."""
        y = self._blob()
        self.lines.append(
            f"Eltwise {self._name('eltwise')} 2 1 {branch} {skip} {y} 0=1 -23301=2,{RESIDUAL_SCALE:e},1.000000e+00"
        )
        return y

    def add(self, a: str, b: str) -> str:
        y = self._blob()
        self.lines.append(f"BinaryOp {self._name('add')} 2 1 {a} {b} {y}")  # op_type default 0 = add, as the official file writes it
        return y

    def upsample_nearest_2x(self, x: str) -> str:
        y = self._blob()
        self.lines.append(f"Interp {self._name('interp')} 1 1 {x} {y} 0=1 1=2.000000e+00 2=2.000000e+00")
        return y

    def param(self) -> str:
        return "7767517\n" + f"{len(self.lines)} {self.blobs}\n" + "\n".join(self.lines) + "\n"


def _conv_params(module: torch.nn.Conv2d) -> tuple[np.ndarray, np.ndarray]:
    return (
        module.weight.detach().float().cpu().numpy(),
        module.bias.detach().float().cpu().numpy(),
    )


def _rdb(e: _Emitter, rdb: torch.nn.Module, x_uses: list[str], residual_skip: str) -> str:
    """ResidualDenseBlock_5C. x_uses: five copies of the block input (conv1
    input + four concats); residual_skip: a sixth copy for the residual."""
    convs = [getattr(rdb, f"conv{i}") for i in range(1, 6)]
    convs = [c[0] if isinstance(c, torch.nn.Sequential) else c for c in convs]
    x1 = e.conv(x_uses[0], *_conv_params(convs[0]), lrelu=True)
    x1s = e.split(x1, 4)
    x2 = e.conv(e.concat([x_uses[1], x1s[0]]), *_conv_params(convs[1]), lrelu=True)
    x2s = e.split(x2, 3)
    x3 = e.conv(e.concat([x_uses[2], x1s[1], x2s[0]]), *_conv_params(convs[2]), lrelu=True)
    x3s = e.split(x3, 2)
    x4 = e.conv(e.concat([x_uses[3], x1s[2], x2s[1], x3s[0]]), *_conv_params(convs[3]), lrelu=True)
    x5 = e.conv(e.concat([x_uses[4], x1s[3], x2s[2], x3s[1], x4]), *_conv_params(convs[4]), lrelu=False)
    return e.residual(x5, residual_skip)


def _rrdb(e: _Emitter, rrdb: torch.nn.Module, x: str, extra_uses: int) -> tuple[str, list[str]]:
    """One RRDB. Returns (output, leftover copies of x for the caller)."""
    uses = e.split(x, 5 + 1 + 1 + extra_uses)
    out1 = _rdb(e, rrdb.RDB1, uses[0:5], uses[5])
    u2 = e.split(out1, 6)
    out2 = _rdb(e, rrdb.RDB2, u2[0:5], u2[5])
    u3 = e.split(out2, 6)
    out3 = _rdb(e, rrdb.RDB3, u3[0:5], u3[5])
    return e.residual(out3, uses[6]), uses[7:]


def esrgan_to_ncnn(model: torch.nn.Module, *, input_blob: str = "data", output_blob: str = "output") -> tuple[str, bytes]:
    """spandrel's RRDBNet (model.model = Sequential[conv_first, ShortcutBlock,
    Upsample, conv, LReLU, Upsample, conv, LReLU, conv, LReLU, conv]) ->
    (param text, bin bytes). Only the x4 layout is handled."""
    seq = model.model
    names = [type(m).__name__ for m in seq]
    expected = ["Conv2d", "ShortcutBlock", "Upsample", "Conv2d", "LeakyReLU", "Upsample", "Conv2d", "LeakyReLU", "Conv2d", "LeakyReLU", "Conv2d"]
    if names != expected:
        raise ValueError(f"not an x4 RRDBNet Sequential: {names}")
    inner = list(seq[1].sub.children())
    blocks, trunk = inner[:-1], inner[-1]
    if not blocks or any(type(b).__name__ != "RRDB" for b in blocks) or type(trunk).__name__ != "Conv2d":
        raise ValueError("ShortcutBlock is not [RRDB..., Conv2d]")

    e = _Emitter()
    x = e.input(input_blob)
    fea = e.conv(x, *_conv_params(seq[0]), lrelu=False)
    # fea feeds block 1 (7 uses) and the trunk skip at the end (1 more).
    cur, leftover = _rrdb(e, blocks[0], fea, extra_uses=1)
    fea_skip = leftover[0]
    for block in blocks[1:]:
        cur, _ = _rrdb(e, block, cur, extra_uses=0)
    trunk_out = e.conv(cur, *_conv_params(trunk), lrelu=False)
    y = e.add(trunk_out, fea_skip)
    y = e.conv(e.upsample_nearest_2x(y), *_conv_params(seq[3]), lrelu=True)
    y = e.conv(e.upsample_nearest_2x(y), *_conv_params(seq[6]), lrelu=True)
    y = e.conv(y, *_conv_params(seq[8]), lrelu=True)
    e.conv(y, *_conv_params(seq[10]), lrelu=False, out_name=output_blob)
    return e.param(), bytes(e.weights)
