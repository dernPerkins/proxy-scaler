"""Unit tests for decklist parsing (no network / GPU required)."""

from proxy_scaler.decklist import parse_line, parse_decklist
from proxy_scaler.pipeline import output_filename
from proxy_scaler.scryfall import expand_faces, _names_compatible
from proxy_scaler.upscale import effective_tile_size as _effective_tile_size
from proxy_scaler.upscale import UpscaleModel


def test_effective_tile_size_heavy_model_auto_default():
    # Not manually set (0) — heavy models fall back to DEFAULT_TILE_SIZE.
    assert _effective_tile_size(UpscaleModel.ILLUSTRATIONJANAI, 0) > 0
    assert _effective_tile_size(UpscaleModel.ULTRASHARP_V2, 0) > 0


def test_effective_tile_size_light_model_stays_off_by_default():
    # Not manually set (0) — lighter models are left untouched (no
    # regression risk for models that already work fine).
    assert _effective_tile_size(UpscaleModel.REALESRGAN_ANIME_FAST, 0) == 0
    # RealPLKSR is a small CNN — untiled like the compact model, not heavy.
    assert _effective_tile_size(UpscaleModel.ULTRASHARP_V2_LITE, 0) == 0


def test_effective_tile_size_explicit_override_always_wins():
    assert _effective_tile_size(UpscaleModel.REALESRGAN_ANIME_FAST, 256) == 256
    assert _effective_tile_size(UpscaleModel.ULTRASHARP_V2, 512) == 512


def test_parse_exact_printing():
    e = parse_line("1 Abandoned Air Temple (tla) 263")
    assert e is not None
    assert e.quantity == 1
    assert e.name == "Abandoned Air Temple"
    assert e.set_code == "tla"
    assert e.collector_number == "263"
    assert e.has_exact_printing


def test_parse_dfc_exact():
    e = parse_line(
        "1 Dion, Bahamut's Dominant // Bahamut, Warden of Light (fin) 376"
    )
    assert e is not None
    assert e.name == "Dion, Bahamut's Dominant // Bahamut, Warden of Light"
    assert e.set_code == "fin"
    assert e.collector_number == "376"


def test_parse_collector_suffixes():
    assert parse_line("1 History of Benalia (pdom) 21p").collector_number == "21p"
    assert parse_line("1 Knight Exemplar (plst) DDG-14").collector_number == "DDG-14"
    assert (
        parse_line("1 Nykthos, Shrine to Nyx (ppro) 2022-3").collector_number
        == "2022-3"
    )


def test_parse_name_only():
    e = parse_line("4 Lightning Bolt")
    assert e is not None
    assert e.quantity == 4
    assert e.name == "Lightning Bolt"
    assert not e.has_exact_printing


def test_parse_qty_with_x_suffix():
    # The "4x Lightning Bolt" style many deck-site exports use.
    e = parse_line("4x Lightning Bolt")
    assert e is not None
    assert e.quantity == 4
    assert e.name == "Lightning Bolt"

    upper = parse_line("2X Sol Ring (c21) 263")
    assert upper is not None
    assert upper.quantity == 2
    assert upper.name == "Sol Ring"
    assert upper.set_code == "c21"


def test_parse_collector_hint_without_set():
    # The best some deck managers can export, notably for non-English
    # cards: a trailing collector number with no set code. Parsed as a
    # *hint* — collector_number set, set_code None, has_exact_printing
    # False — so resolution matches it against the name's printings and
    # can still fall back to the plain name path.
    e = parse_line("1 Sol Ring 263")
    assert e is not None
    assert e.name == "Sol Ring"
    assert e.set_code is None
    assert e.collector_number == "263"
    assert not e.has_exact_printing

    suffixed = parse_line("2 History of Benalia 21p")
    assert suffixed.name == "History of Benalia"
    assert suffixed.collector_number == "21p"


def test_parse_collector_hint_leaves_real_names_alone():
    # Tokens that aren't strictly digits(+one letter) stay part of the
    # name — a looser match would eat the ends of real card names.
    arrows = parse_line("1 Borrowing 100,000 Arrows")
    assert arrows.name == "Borrowing 100,000 Arrows"
    assert arrows.collector_number is None

    # A bare number alone is a name, not an empty name + hint.
    bare = parse_line("1 234")
    assert bare.name == "234"
    assert bare.collector_number is None


def test_parse_skip_headers():
    assert parse_line("Deck") is None
    assert parse_line("") is None
    assert parse_line("# comment") is None


def test_parse_qty_plains():
    e = parse_line("20 Plains (mh2) 482")
    assert e is not None
    assert e.quantity == 20
    assert e.name == "Plains"


def test_output_filename():
    assert (
        output_filename("Sol Ring", "c21", "263", None)
        == "Sol_Ring-C21-263.png"
    )
    assert (
        output_filename("Dion, Bahamut's Dominant", "fin", "376", "front")
        == "Dion_Bahamuts_Dominant-FIN-376-front.png"
    )
    assert (
        output_filename("Sol Ring", "c21", "263", None, "ultrasharp_v2")
        == "Sol_Ring-C21-263-ultrasharp_v2.png"
    )
    assert (
        output_filename("Sol Ring", "c21", "263", None, "ultrasharp_v2", 800)
        == "Sol_Ring-C21-263-ultrasharp_v2-800dpi.png"
    )
    assert (
        output_filename("Sol Ring", "c21", "263", "front", "illustrationjanai", 1200)
        == "Sol_Ring-C21-263-front-illustrationjanai-1200dpi.png"
    )


def test_names_compatible():
    assert _names_compatible("Sol Ring", "Sol Ring")
    assert _names_compatible(
        "Dion, Bahamut's Dominant",
        "Dion, Bahamut's Dominant // Bahamut, Warden of Light",
    )


def test_expand_faces_single():
    card = {
        "id": "abc",
        "name": "Sol Ring",
        "set": "c21",
        "collector_number": "263",
        "image_status": "highres_scan",
        "image_uris": {"png": "https://example.com/sol.png"},
    }
    faces = expand_faces(card)
    assert len(faces) == 1
    assert faces[0].face_index is None
    assert faces[0].png_url.endswith("sol.png")


def test_expand_faces_dfc():
    card = {
        "id": "dfc",
        "name": "Front // Back",
        "set": "fin",
        "collector_number": "376",
        "card_faces": [
            {"name": "Front", "image_uris": {"png": "https://example.com/f.png"}},
            {"name": "Back", "image_uris": {"png": "https://example.com/b.png"}},
        ],
    }
    faces = expand_faces(card)
    assert len(faces) == 2
    assert faces[0].face_label == "front"
    assert faces[1].face_label == "back"


def test_expand_faces_split_single_image():
    """Split cards have card_faces but image_uris only on the parent."""
    card = {
        "id": "split",
        "name": "Fire // Ice",
        "set": "mh2",
        "collector_number": "1",
        "image_uris": {"png": "https://example.com/split.png"},
        "card_faces": [
            {"name": "Fire"},
            {"name": "Ice"},
        ],
    }
    faces = expand_faces(card)
    assert len(faces) == 1
    assert faces[0].face_index is None
