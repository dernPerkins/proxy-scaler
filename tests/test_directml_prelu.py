"""The torch-directml channel-wise PReLU workaround (see upscale.py's
"DirectML: channel-wise PReLU workaround" section). No DirectML hardware
exists on the CI/dev boxes, so these pin down everything that CAN be
checked here: the replacement is arithmetically identical to nn.PReLU,
the patcher swaps exactly the right layers of the real Compact
architecture, and the load-time policy routes each backend correctly."""

from __future__ import annotations

import torch
from spandrel.architectures.Compact import Compact

from proxy_scaler import upscale as upscale_module
from proxy_scaler.upscale import (
    DIRECTML_PRELU_DEFAULT,
    DIRECTML_PRELU_ENV,
    Upscaler,
    UpscaleModel,
    _make_directml_safe_prelu,
    apply_directml_prelu_policy,
    directml_prelu_policy,
    has_channelwise_prelu,
    replace_channelwise_prelu,
)


class _DirectMLDevice:
    """torch.device("privateuseone") can't be constructed without the
    torch_directml plugin registering the backend, so a stand-in with the
    same .type is what every test here (and test_oom's device_kind test)
    uses."""

    type = "privateuseone"

    def __str__(self) -> str:
        return "privateuseone:0"


def _tiny_compact(seed: int = 0) -> Compact:
    """Same architecture family as realesr-animevideov3 (which is 64
    features x 16 convs), just small: 8 features, 2 convs -> 3 channel-wise
    PReLU layers with 8 slopes each."""
    torch.manual_seed(seed)
    model = Compact(num_in_ch=3, num_out_ch=3, num_feat=8, num_conv=2, upscale=4)
    # Default PReLU init is a constant 0.25 slope; randomize so a wrong
    # broadcast (e.g. slopes applied along the wrong dim) can't hide.
    with torch.no_grad():
        for m in model.modules():
            if isinstance(m, torch.nn.PReLU):
                m.weight.copy_(torch.rand_like(m.weight) * 2 - 1)
    return model.eval()


def test_safe_prelu_matches_nn_prelu_channelwise() -> None:
    torch.manual_seed(1)
    original = torch.nn.PReLU(num_parameters=5)
    with torch.no_grad():
        original.weight.copy_(torch.tensor([-0.5, 0.0, 0.25, 1.0, 2.0]))
    safe = _make_directml_safe_prelu(original)
    x = torch.randn(2, 5, 7, 6) * 3

    assert torch.allclose(safe(x), original(x), atol=1e-6)
    # Exact on the sign boundary too: relu(0) - w*relu(-0) is 0.
    assert torch.equal(safe(torch.zeros(1, 5, 2, 2)), torch.zeros(1, 5, 2, 2))
    # 2-D input (N, C): the slopes still broadcast along dim 1.
    x2 = torch.randn(4, 5)
    assert torch.allclose(safe(x2), original(x2), atol=1e-6)


def test_safe_prelu_shares_the_original_parameter() -> None:
    original = torch.nn.PReLU(num_parameters=3)
    safe = _make_directml_safe_prelu(original)
    assert safe.weight is original.weight
    assert safe.num_parameters == 3
    # .to() moves/casts the shared weight like any parameter would.
    safe.to(torch.float64)
    assert safe.weight.dtype == torch.float64
    assert "DirectML-safe" in repr(safe)


def test_replace_swaps_only_channelwise_prelu_and_keeps_output() -> None:
    model = _tiny_compact()
    x = torch.rand(1, 3, 12, 10)
    with torch.inference_mode():
        before = model(x)
    assert has_channelwise_prelu(model)

    swapped = replace_channelwise_prelu(model)

    assert swapped == 3  # first activation + one per body conv
    assert not has_channelwise_prelu(model)
    assert not any(isinstance(m, torch.nn.PReLU) for m in model.modules())
    with torch.inference_mode():
        after = model(x)
    assert torch.allclose(before, after, atol=1e-6)
    # The swap is by module *name*, so the state dict is unchanged — the
    # loaded weights and the ModuleList indices are exactly as before.
    assert set(model.state_dict().keys()) == set(_tiny_compact().state_dict().keys())


def test_replace_leaves_single_parameter_prelu_and_other_models_alone() -> None:
    single = torch.nn.Sequential(torch.nn.Linear(4, 4), torch.nn.PReLU())
    assert not has_channelwise_prelu(single)
    assert replace_channelwise_prelu(single) == 0
    assert isinstance(single[1], torch.nn.PReLU)

    gelu = torch.nn.Sequential(torch.nn.Conv2d(3, 3, 1), torch.nn.GELU())
    assert replace_channelwise_prelu(gelu) == 0


def test_policy_env_parsing() -> None:
    assert directml_prelu_policy({}) == DIRECTML_PRELU_DEFAULT == "patch"
    assert directml_prelu_policy({DIRECTML_PRELU_ENV: "CPU "}) == "cpu"
    assert directml_prelu_policy({DIRECTML_PRELU_ENV: "off"}) == "off"
    # A typo must never fall through to the aborting native path.
    assert directml_prelu_policy({DIRECTML_PRELU_ENV: "none"}) == "patch"


def test_policy_patch_keeps_directml_device_and_patches(capsys) -> None:
    model = _tiny_compact()
    device = _DirectMLDevice()

    out = apply_directml_prelu_policy(model, device, policy="patch")  # type: ignore[arg-type]

    assert out is device
    assert not has_channelwise_prelu(model)
    assert "swapped 3 channel-wise PReLU" in capsys.readouterr().out


def test_policy_cpu_reroutes_without_patching(capsys) -> None:
    model = _tiny_compact()

    out = apply_directml_prelu_policy(model, _DirectMLDevice(), policy="cpu")  # type: ignore[arg-type]

    assert out == torch.device("cpu")
    assert has_channelwise_prelu(model)  # untouched: nn.PReLU is fine on CPU
    assert "running it on the CPU" in capsys.readouterr().out


def test_policy_off_warns_and_leaves_everything(capsys) -> None:
    model = _tiny_compact()
    device = _DirectMLDevice()

    out = apply_directml_prelu_policy(model, device, policy="off")  # type: ignore[arg-type]

    assert out is device
    assert has_channelwise_prelu(model)
    assert "known to abort" in capsys.readouterr().err


def test_policy_is_a_no_op_off_directml_and_for_prelu_free_models(capsys, monkeypatch) -> None:
    monkeypatch.setenv(DIRECTML_PRELU_ENV, "cpu")
    # CUDA/MPS/CPU never see the workaround, whatever the env says.
    for dev in (torch.device("cuda"), torch.device("mps"), torch.device("cpu")):
        model = _tiny_compact()
        assert apply_directml_prelu_policy(model, dev) is dev
        assert has_channelwise_prelu(model)
    # And a PReLU-free model on DirectML (the DAT2 / RealPLKSR case) is
    # left alone on the GPU even under the cpu policy.
    device = _DirectMLDevice()
    gelu = torch.nn.Sequential(torch.nn.Conv2d(3, 3, 1), torch.nn.GELU())
    assert apply_directml_prelu_policy(gelu, device) is device  # type: ignore[arg-type]
    assert capsys.readouterr().out == ""


def test_load_model_runs_policy_before_moving_to_device(tmp_path, monkeypatch) -> None:
    """_load_model must apply the policy on the loaded model BEFORE
    .to(device): with the cpu policy the weights never touch the GPU, and
    with the patch policy the swapped layers are what gets moved."""
    import spandrel

    calls: list[tuple[str, str]] = []
    model = _tiny_compact()

    class _FakeDescriptor:
        def __init__(self) -> None:
            self.model = model

        def to(self, target):
            calls.append(("to", str(getattr(target, "type", target))))
            return self

        def eval(self):
            return self

    fake = _FakeDescriptor()

    class _FakeLoader:
        def load_from_file(self, path):
            return fake

    weights = tmp_path / "w.pth"
    weights.write_bytes(b"x")
    monkeypatch.setattr(upscale_module, "ensure_weights", lambda *a, **k: weights)
    monkeypatch.setattr(upscale_module, "resolve_device", lambda: _DirectMLDevice())
    monkeypatch.setattr(upscale_module, "resolve_dtype", lambda d, dev: torch.float32)
    monkeypatch.setattr(spandrel, "ModelLoader", _FakeLoader)
    monkeypatch.setattr(spandrel, "ImageModelDescriptor", _FakeDescriptor, raising=False)
    monkeypatch.setattr(upscale_module, "ImageModelDescriptor", _FakeDescriptor, raising=False)

    up = Upscaler(model=UpscaleModel.REALESRGAN_ANIME_FAST, scale=4, weights_dir=tmp_path)

    monkeypatch.setenv(DIRECTML_PRELU_ENV, "patch")
    assert up._load_model() is fake
    assert not has_channelwise_prelu(model)  # patched before the move…
    assert calls == [("to", "privateuseone")]  # …and still bound for the GPU

    calls.clear()
    fake.model = _tiny_compact()
    monkeypatch.setenv(DIRECTML_PRELU_ENV, "cpu")
    up._load_model()
    assert calls == [("to", "cpu")]
    assert has_channelwise_prelu(fake.model)
