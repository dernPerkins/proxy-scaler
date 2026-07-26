"""Integration-style tests with mocked Scryfall HTTP (no live network)."""

from __future__ import annotations

from pathlib import Path

from proxy_scaler.cli import main
from proxy_scaler.decklist import parse_decklist
from proxy_scaler.scryfall import ScryfallClient, expand_faces


DFC_CARD = {
    "id": "dfc-id",
    "name": "Dion, Bahamut's Dominant // Bahamut, Warden of Light",
    "set": "fin",
    "collector_number": "376",
    "image_status": "highres_scan",
    "card_faces": [
        {
            "name": "Dion, Bahamut's Dominant",
            "image_uris": {"png": "https://example.com/front.png"},
        },
        {
            "name": "Bahamut, Warden of Light",
            "image_uris": {"png": "https://example.com/back.png"},
        },
    ],
}

SOL_RING = {
    "id": "sol-id",
    "name": "Sol Ring",
    "set": "c21",
    "collector_number": "263",
    "image_status": "highres_scan",
    "image_uris": {"png": "https://example.com/sol.png"},
}


def test_example_decklist_parses():
    path = Path(__file__).resolve().parents[1] / "cards.example.txt"
    entries = parse_decklist(path)
    assert len(entries) >= 80
    exact = [e for e in entries if e.has_exact_printing]
    name_only = [e for e in entries if not e.has_exact_printing]
    assert any(e.collector_number == "DDG-14" for e in exact)
    assert any(e.name == "Sol Ring" for e in name_only)
    dfc = next(e for e in exact if "Dion" in e.name)
    assert dfc.set_code == "fin"


def test_dfc_expands_two_faces():
    faces = expand_faces(DFC_CARD)
    assert len(faces) == 2
    assert faces[0].face_label == "front"
    assert faces[1].face_label == "back"


def test_resolve_exact_and_fuzzy(monkeypatch):
    client = ScryfallClient(delay_s=0)

    def fake_get(path, params=None):
        if path.startswith("/cards/c21/"):
            return SOL_RING
        if path == "/cards/named":
            return SOL_RING
        raise AssertionError(f"unexpected path {path} {params}")

    monkeypatch.setattr(client, "_get", fake_get)

    from proxy_scaler.decklist import parse_line

    card, warns = client.resolve(parse_line("1 Sol Ring (c21) 263"))
    assert card["name"] == "Sol Ring"
    assert warns == []

    card2, warns2 = client.resolve(parse_line("1 Sol Ring"))
    assert card2["set"] == "c21"
    assert any("Name-only" in w for w in warns2)


def test_cli_writes_outputs_with_mocks(tmp_path, monkeypatch):
    deck = tmp_path / "deck.txt"
    deck.write_text(
        "1 Sol Ring (c21) 263\n"
        "1 Dion, Bahamut's Dominant // Bahamut, Warden of Light (fin) 376\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    cache = tmp_path / "cache"

    # Tiny valid PNG (1x1)
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    def fake_resolve(entry):
        if entry.set_code == "fin":
            return DFC_CARD, []
        return SOL_RING, []

    monkeypatch.setattr(ScryfallClient, "resolve", lambda self, e: fake_resolve(e))
    monkeypatch.setattr(
        "proxy_scaler.pipeline.download_png", lambda url, session=None: png_bytes
    )

    fake_upscaled = Image.new("RGB", (16, 16), color=(40, 50, 60))

    class FakeUpscaler:
        def __init__(self, model="swinir", scale=4, weights_dir="weights", **_kw):
            from proxy_scaler.upscale import UpscaleModel

            self.model_id = UpscaleModel(model) if isinstance(model, str) else model
            self.scale = scale

        def upscale(self, image):
            from proxy_scaler.upscale import UpscaleResult

            return UpscaleResult(image=fake_upscaled, device="gpu")

    monkeypatch.setattr("proxy_scaler.pipeline.Upscaler", FakeUpscaler)

    rc = main(
        [
            str(deck),
            "-o",
            str(out),
            "--cache-dir",
            str(cache),
            "--dpi",
            "800",
            "--model",
            "swinir",
        ]
    )
    assert rc == 0
    names = sorted(p.name for p in out.glob("*.png"))
    assert "Sol_Ring-C21-263-swinir-800dpi.png" in names
    assert "Dion_Bahamuts_Dominant-FIN-376-front-swinir-800dpi.png" in names
    assert "Bahamut_Warden_of_Light-FIN-376-back-swinir-800dpi.png" in names
    assert len(names) == 3
