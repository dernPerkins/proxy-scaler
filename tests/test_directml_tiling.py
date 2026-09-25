"""DirectML-only upscaler behaviour, from the RX 9070 XT report: tiles cut
from sub-regions of a larger device buffer came back black or as noise
(every model, every tile size), DirectML's out-of-memory message wasn't
recognised, and a failed task kept VRAM / parked the model on the CPU.

No DirectML here: the device stays the CPU and `_is_directml_device` is
forced true, which routes through exactly the DirectML code paths."""

from __future__ import annotations

import torch
import torch.nn.functional as F
from PIL import Image

import pytest

from proxy_scaler import upscale as up
from proxy_scaler.upscale import Upscaler, UpscaleModel, _is_oom_error


def _nn4(x: torch.Tensor) -> torch.Tensor:
    """A position-independent x4 'model': nearest-neighbour upscale."""
    return F.interpolate(x, scale_factor=4, mode="nearest")


class FakeDescriptor:
    """Callable like a spandrel descriptor. `broken(call_no, tile)` picks a
    failure mode per call: None, "black", "nan", "noise" or "oom"."""

    def __init__(self, broken=lambda n, t: None, require_contiguous=True):
        self.broken = broken
        self.require_contiguous = require_contiguous
        self.calls: list[tuple[int, int]] = []
        self.contiguous: list[bool] = []

    def __call__(self, x: torch.Tensor) -> torch.Tensor:
        n = len(self.calls)
        self.calls.append(tuple(x.shape[-2:]))
        self.contiguous.append(x.is_contiguous())
        if self.require_contiguous:
            assert x.is_contiguous(), "DirectML must only ever see standalone tiles"
        out = _nn4(x)
        mode = self.broken(n, x)
        if mode == "black":
            return torch.zeros_like(out)
        if mode == "nan":
            return torch.full_like(out, float("nan"))
        if mode == "noise":
            return torch.rand_like(out)
        if mode == "oom":
            raise RuntimeError("There is not enough GPU video memory available!")
        return out


@pytest.fixture
def directml(monkeypatch):
    monkeypatch.setattr(up, "_is_directml_device", lambda device: True)


def _card(w=90, h=120) -> Image.Image:
    small = Image.new("RGB", (6, 8))
    small.putdata([((x * 40) % 256, (y * 30) % 256, 128) for y in range(8) for x in range(6)])
    img = small.resize((w, h), Image.Resampling.BICUBIC).convert("RGBA")
    img.putpixel((0, 0), (0, 0, 0, 0))
    return img


def _upscaler(tile: int) -> Upscaler:
    u = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir="w", tile=tile, tile_pad=4)
    u._device = torch.device("cpu")
    u._dtype = torch.float32
    return u


def _run(u: Upscaler, descriptor: FakeDescriptor, img: Image.Image):
    from unittest.mock import patch

    u._descriptor = descriptor
    with patch.object(u, "_ensure_model", return_value=descriptor):
        return u.upscale(img)


def test_directml_tiling_matches_untiled_and_only_sends_standalone_tiles(directml):
    img = _card()
    whole = _run(_upscaler(0), FakeDescriptor(), img).image
    d = FakeDescriptor()
    tiled = _run(_upscaler(32), d, img).image
    assert tiled.size == whole.size == (360, 480)
    assert list(tiled.getdata()) == list(whole.getdata())
    # 90x120 at tile 32 -> 3 columns x 4 rows, each a separate call.
    assert len(d.calls) == 12


@pytest.mark.parametrize("mode", ["black", "nan", "noise"])
def test_bad_tile_is_retried_on_the_device(directml, mode, capsys):
    """The exact report: a left-column tile below the first row comes back
    broken once; it's redone and the card is correct."""
    fired = []

    def broken(n, x):
        if n == 3 and not fired:  # 4th tile = column 0, row 1 at this geometry
            fired.append(n)
            return mode
        return None

    good = _run(_upscaler(32), FakeDescriptor(), _card()).image
    d = FakeDescriptor(broken)
    result = _run(_upscaler(32), d, _card())
    assert list(result.image.getdata()) == list(good.getdata())
    assert len(d.calls) == 13  # one extra call: the retry
    out = capsys.readouterr().out
    assert "retrying once on the GPU" in out and "came back fine on retry" in out
    assert "WARNING" not in out


def test_tile_broken_twice_is_kept_and_called_out(directml, capsys):
    """No CPU fallback: a tile that fails its retry too is kept exactly as
    the device returned it (black here), and the log says so loudly."""
    d = FakeDescriptor(lambda n, x: "black" if n in (3, 4) else None)  # tile + its retry
    good = _run(_upscaler(32), FakeDescriptor(), _card()).image
    result = _run(_upscaler(32), d, _card())
    assert len(d.calls) == 13  # 12 tiles + one retry, nothing more
    assert list(result.image.getdata()) != list(good.getdata())
    # Tile 3 is column 0, row 1: its unpadded core stays black in the card.
    assert result.image.getpixel((40, 150))[:3] == (0, 0, 0)
    out = capsys.readouterr().out
    assert "retrying once on the GPU" in out
    assert "WARNING directml" in out and "please report it" in out


def test_black_input_region_is_not_flagged(directml):
    """A genuinely black part of the card must not be mistaken for a
    failed tile: the check compares the output with its own input."""
    img = Image.new("RGB", (80, 80), (0, 0, 0))
    d = FakeDescriptor()
    result = _run(_upscaler(32), d, img)
    assert len(d.calls) == 9  # 3x3 tiles, no retries
    assert result.image.getextrema() == ((0, 0), (0, 0), (0, 0))


def test_directml_oom_message_is_an_oom():
    assert _is_oom_error(RuntimeError("There is not enough GPU video memory available!"))
    assert _is_oom_error(RuntimeError("DirectML: E_OUTOFMEMORY"))
    assert _is_oom_error(RuntimeError("failed with HRESULT 0x8007000E"))
    assert not _is_oom_error(RuntimeError("The GPU device instance has been suspended"))


def test_directml_oom_walks_the_tile_ladder(directml, monkeypatch):
    """Auto tiling on DirectML: DirectML's own OOM message at the heavy
    default (384) steps down the ladder on the same device instead of
    failing the task outright."""
    monkeypatch.setattr(up, "_clear_device_cache", lambda device: None)
    u = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir="w", tile=384, tile_auto=True)
    # The fake device is the CPU, which _try_gpu_inference (rightly) never
    # treats as retryable; this stand-in keeps its logic minus that guard.
    u._device = torch.device("cpu")
    u._dtype = torch.float32
    d = FakeDescriptor(lambda n, x: "oom" if max(x.shape[-2:]) > 330 else None)

    def try_as_gpu(self, descriptor, tensor):
        try:
            return self._run_inference(descriptor, tensor)
        except Exception as exc:
            if not _is_oom_error(exc):
                raise
        return None

    monkeypatch.setattr(Upscaler, "_try_gpu_inference", try_as_gpu)
    result = _run(u, d, _card(600, 800))
    assert result.image.size == (2400, 3200)
    assert u.tile < 384


def test_clear_device_cache_collects_garbage_on_directml(monkeypatch):
    import gc

    calls = []
    monkeypatch.setattr(gc, "collect", lambda *a: calls.append(1) or 0)

    class _Dml:
        type = "privateuseone"

    up._clear_device_cache(_Dml())  # type: ignore[arg-type]
    assert calls == [1]


def test_directml_model_parked_on_cpu_returns_to_gpu(monkeypatch, tmp_path):
    """After an OOM parked the model on the CPU, the next task tries the
    DirectML device again (there's no VRAM probe to wait on)."""

    class _Dml:
        type = "privateuseone"

    class _Desc:
        def __init__(self):
            self.model = torch.nn.Linear(1, 1)

    desc = _Desc()
    key = up._cache_key(UpscaleModel.ULTRASHARP_V2, 4, tmp_path)
    monkeypatch.setattr(up, "_MODEL_CACHE", {key: desc})
    monkeypatch.setattr(up, "resolve_device", lambda: _Dml())
    moved = []

    def fake_return(self, descriptor, device):
        moved.append(device.type)
        return descriptor

    monkeypatch.setattr(Upscaler, "_return_to_gpu", fake_return)
    u = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir=tmp_path)
    u._ensure_model()
    assert moved == ["privateuseone"]


def test_tiled_resources_switch(monkeypatch, capsys):
    class _FakeDml:
        def __init__(self):
            self.calls = []

        def disable_tiled_resources(self, flag):
            self.calls.append(flag)

    fake = _FakeDml()
    monkeypatch.setattr(up, "_DIRECTML_TILED_RESOURCES_APPLIED", False)
    monkeypatch.delenv(up.DIRECTML_TILED_RESOURCES_ENV, raising=False)
    up._apply_directml_tiled_resources_policy(fake)
    assert fake.calls == []  # default: untouched

    monkeypatch.setattr(up, "_DIRECTML_TILED_RESOURCES_APPLIED", False)
    monkeypatch.setenv(up.DIRECTML_TILED_RESOURCES_ENV, "off")
    up._apply_directml_tiled_resources_policy(fake)
    up._apply_directml_tiled_resources_policy(fake)  # once per process
    assert fake.calls == [True]


def test_non_directml_paths_are_untouched():
    """CUDA/MPS/CPU still use the original on-device tiling: the DirectML
    check says no and _tiled_inference is what runs."""
    from unittest.mock import patch

    u = _upscaler(32)
    d = FakeDescriptor(require_contiguous=False)
    with patch.object(Upscaler, "_directml_tiled_inference") as dml:
        result = _run(u, d, _card())
    dml.assert_not_called()
    assert result.image.size == (360, 480)
    # The original path hands the model views of the on-device image.
    assert not all(d.contiguous)


# --- localized DirectML errors + worker recycle (Ofni, French Windows) -----


def _french_oom() -> UnicodeDecodeError:
    raw = "Il n’y a pas assez de mémoire vidéo".encode("cp1252")
    return UnicodeDecodeError("utf-8", raw, 4, 5, "invalid start byte")


def test_lost_directml_message_is_recovered():
    exc = _french_oom()
    assert "0x92" in str(exc)  # what the Tasks tab showed
    assert up._lost_directml_message(exc) == "Il n’y a pas assez de mémoire vidéo"
    assert up._lost_directml_message(RuntimeError("x")) is None


def test_undecodable_error_counts_as_memory_error_on_directml_only(monkeypatch, capsys):
    class _Dml:
        type = "privateuseone"

    assert up._is_device_memory_error(_french_oom(), _Dml())
    assert "pas assez de mémoire" in capsys.readouterr().out
    assert not up._is_device_memory_error(_french_oom(), torch.device("cpu"))
    assert up._is_device_memory_error(RuntimeError("CUDA out of memory"), torch.device("cpu"))


def test_failed_directml_task_requests_a_worker_recycle(directml, monkeypatch):
    monkeypatch.setattr(up, "_WORKER_RECYCLE_REASON", None)

    class Boom(FakeDescriptor):
        def __call__(self, x):
            raise _french_oom()

    with pytest.raises(UnicodeDecodeError):
        _run(_upscaler(0), Boom(), _card())
    assert up.worker_recycle_reason() is not None
    assert "mémoire" in up.worker_recycle_reason()


def test_successful_directml_task_does_not_request_recycle(directml, monkeypatch):
    monkeypatch.setattr(up, "_WORKER_RECYCLE_REASON", None)
    _run(_upscaler(32), FakeDescriptor(), _card())
    assert up.worker_recycle_reason() is None


def test_directml_oom_on_the_ladder_requests_a_recycle(monkeypatch):
    """The real _try_gpu_inference: a French DirectML OOM is recognised
    (returns None, so the tile ladder steps down) and asks for a worker
    recycle; a CUDA-side non-OOM error still propagates untouched."""
    monkeypatch.setattr(up, "_WORKER_RECYCLE_REASON", None)

    class _Dml:
        type = "privateuseone"

    u = Upscaler(model=UpscaleModel.ULTRASHARP_V2, scale=4, weights_dir="w", tile=384)
    u._device = _Dml()

    def raise_french(descriptor, tensor):
        raise _french_oom()

    monkeypatch.setattr(u, "_run_inference", raise_french)
    assert u._try_gpu_inference(object(), torch.zeros(1)) is None
    assert "out of memory on DirectML" in up.worker_recycle_reason()

    monkeypatch.setattr(up, "_WORKER_RECYCLE_REASON", None)
    u._device = torch.device("cuda")
    monkeypatch.setattr(u, "_run_inference", lambda d, t: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ValueError):
        u._try_gpu_inference(object(), torch.zeros(1))
    assert up.worker_recycle_reason() is None


def test_worker_exits_for_recycle_after_finishing_the_task(tmp_path, monkeypatch):
    """The worker lets the task's finish land, then exits with the
    recycle code so the supervisor starts a fresh process."""
    from proxy_scaler import supervisor, worker

    assert worker.WORKER_RECYCLE_EXIT_CODE == supervisor.WORKER_RECYCLE_EXIT_CODE
    finished = []

    class _Task:
        id = 1
        face_name = "Sol Ring"
        dpi = 1200
        model = "ultrasharp_v2"
        scryfall_id = "x"
        face_index = None

    tasks = [_Task()]
    monkeypatch.setattr(worker.db, "acquire_worker_lock", lambda lock_path: 99)
    monkeypatch.setattr(worker.db, "release_worker_lock", lambda fd: None)
    monkeypatch.setattr(worker, "_wait_while_held", lambda db_path: None)
    monkeypatch.setattr(
        worker.db, "reset_orphaned_running_tasks",
        lambda db_path: type("R", (), {"requeued": 0, "failed": 0})(),
    )
    monkeypatch.setattr(worker.db, "clear_cpu_fallback", lambda db_path: None)
    monkeypatch.setattr(worker.db, "claim_next_task", lambda db_path: tasks.pop() if tasks else None)
    monkeypatch.setattr(worker, "_OriginalPrefetcher", lambda db_path: type("P", (), {"kick": lambda self, **k: None})())
    monkeypatch.setattr(worker, "_start_one", lambda task, db_path: (lambda: finished.append(task.id)))
    monkeypatch.setattr(up, "_WORKER_RECYCLE_REASON", "out of memory on DirectML (test)")
    with pytest.raises(SystemExit) as info:
        worker.main(db_path=tmp_path / "db", lock_path=tmp_path / "lock")
    assert info.value.code == worker.WORKER_RECYCLE_EXIT_CODE
    assert finished == [1]
