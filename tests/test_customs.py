"""Custom Images: user-uploaded card fronts.

Covers the two things this feature added that nothing else in the suite
would catch — the content-addressed store's validation/cropping, and the
identity split that lets a task or registry row be keyed by a content hash
instead of a Scryfall UUID (db migration 008).
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from proxy_scaler import customs, db as db_module
from proxy_scaler.db import enqueue_task, init_db, list_gallery_items, parse_output_filename
from proxy_scaler.decklist import DeckEntry
from proxy_scaler.dpi import CARD_HEIGHT_MM, CARD_WIDTH_MM, CUSTOM_SOURCE_MODEL, ORIGINAL_MODEL
from proxy_scaler.pdf_layout import match_quantities
from proxy_scaler.pipeline import FaceResult, face_group_key, output_filename
from proxy_scaler.upscale import cache_path, original_cache_path

HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _png_bytes(width: int, height: int, colour=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


def _custom_result(custom_hash: str, *, dpi: int, model: str, **over) -> FaceResult:
    defaults = dict(
        out_path=Path(f"/out/{custom_hash[:8]}-{dpi}.png"),
        original_path=Path(f"/cache/originals/custom_{custom_hash}_single.png"),
        scryfall_id=None,
        custom_hash=custom_hash,
        face_index=None,
        face_name="My Alter",
        card_name="My Alter",
        set_code="",
        collector_number="",
        png_url="",
        dpi=dpi,
        model=model,
        total_faces=1,
    )
    defaults.update(over)
    return FaceResult(**defaults)


# --------------------------------------------------------------------
# The content-addressed store
# --------------------------------------------------------------------


def test_store_original_round_trips_and_is_idempotent(tmp_path: Path) -> None:
    data = _png_bytes(630, 880)
    expected = hashlib.sha256(data).hexdigest()

    content_hash, path = customs.store_original(data, root=tmp_path)

    assert content_hash == expected
    assert path.is_file()
    assert customs.has_original(content_hash, root=tmp_path)
    # Re-uploading identical bytes is the no-op that makes the client's
    # "sync on miss" cheap to call unconditionally.
    assert customs.store_original(data, root=tmp_path) == (content_hash, path)


def test_store_original_rejects_hash_mismatch(tmp_path: Path) -> None:
    """The hash in the URL is checked against the bytes received, so a
    truncated upload is rejected rather than cached forever under a name
    that lies about its contents."""
    with pytest.raises(customs.CustomImageError):
        customs.store_original(_png_bytes(63, 88), root=tmp_path, expected_hash=HASH_A)
    assert not customs.has_original(HASH_A, root=tmp_path)


def test_store_original_rejects_oversize_and_non_images(tmp_path: Path) -> None:
    too_big = b"x" * (customs.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(customs.CustomImageError, match="larger than"):
        customs.store_original(too_big, root=tmp_path)
    with pytest.raises(customs.CustomImageError, match="could not be read"):
        customs.store_original(b"definitely not an image", root=tmp_path)
    with pytest.raises(customs.CustomImageError, match="empty"):
        customs.store_original(b"", root=tmp_path)


@pytest.mark.parametrize(
    "size",
    [(1000, 1000), (2000, 1000), (600, 1600), (630, 880)],
    ids=["square", "landscape", "tall", "already-card-shaped"],
)
def test_store_original_cover_crops_to_card_aspect(tmp_path: Path, size) -> None:
    """Every stored custom image comes out 63:88, whatever went in — which
    is what lets the upscaler, the PDF renderer and the ZIP export all skip
    having a special case for user-supplied art."""
    content_hash, path = customs.store_original(_png_bytes(*size), root=tmp_path)

    with Image.open(path) as img:
        w, h = img.size
    assert w / h == pytest.approx(CARD_WIDTH_MM / CARD_HEIGHT_MM, rel=1e-3)
    # Cover, not contain: the crop never scales past the source, so the
    # limiting axis keeps its full resolution.
    assert w <= size[0] and h <= size[1]


def test_store_original_never_upscales_a_small_image(tmp_path: Path) -> None:
    _, path = customs.store_original(_png_bytes(126, 176), root=tmp_path)
    with Image.open(path) as img:
        assert img.size == (126, 176)


def test_source_dpi_measures_against_card_height(tmp_path: Path) -> None:
    # 1040px tall over 88mm ≈ 300 DPI, the same measure the client uses.
    content_hash, _ = customs.store_original(_png_bytes(745, 1040), root=tmp_path)
    assert customs.source_dpi(content_hash, root=tmp_path) == pytest.approx(300, abs=1)
    assert customs.source_dpi(HASH_B, root=tmp_path) is None


def test_validate_hash_rejects_path_traversal() -> None:
    """The hash reaches the filesystem, so it is never interpolated
    unchecked."""
    for bad in ["../../etc/passwd", "", "abc", "/" + "a" * 63, "g" * 64, "a" * 63]:
        with pytest.raises(customs.CustomImageError):
            customs.validate_hash(bad)
    # Case and surrounding whitespace are normalised, not rejected — the
    # same bytes must not file under two different names.
    assert customs.validate_hash("  " + "A" * 64 + "\n") == "a" * 64


def test_identity_key_is_the_one_shared_form() -> None:
    assert customs.identity_key("uuid-here", None) == "uuid-here"
    assert customs.identity_key(None, HASH_A) == f"custom:{HASH_A}"
    with pytest.raises(ValueError):
        customs.identity_key(None, None)


# --------------------------------------------------------------------
# Identity in the database (migration 008)
# --------------------------------------------------------------------


def test_enqueue_task_accepts_a_custom_hash_instead_of_a_scryfall_id(db_path: Path) -> None:
    task_id = enqueue_task(
        "tag-a",
        custom_hash=HASH_A,
        face_index=None,
        face_label=None,
        face_name="My Alter",
        card_name="My Alter",
        dpi=1200,
        model="ultrasharp_v2",
        output_dir="/tmp/out",
        cache_dir="/tmp/cache",
        weights_dir="/tmp/weights",
        db_path=db_path,
    )
    task = db_module.get_task(task_id, db_path=db_path)
    assert task.scryfall_id is None
    assert task.custom_hash == HASH_A
    assert task.is_custom
    assert task.identity_key == f"custom:{HASH_A}"
    # No printing, and no URL to fetch from.
    assert task.set_code is None and task.collector_number is None
    assert task.png_url is None


def test_enqueue_task_requires_exactly_one_identity(db_path: Path) -> None:
    common = dict(
        face_index=None,
        face_label=None,
        face_name="X",
        card_name="X",
        dpi=1200,
        model="ultrasharp_v2",
        output_dir="/o",
        cache_dir="/c",
        weights_dir="/w",
        db_path=db_path,
    )
    with pytest.raises(ValueError):
        enqueue_task("tag", **common)
    with pytest.raises(sqlite3.IntegrityError):
        enqueue_task("tag", scryfall_id="sol-id", custom_hash=HASH_A, **common)


def test_registry_keeps_custom_and_scryfall_variants_apart(db_path: Path) -> None:
    db_module.upsert_gallery_item(
        "tag-a", _custom_result(HASH_A, dpi=1200, model="ultrasharp_v2"), db_path=db_path
    )
    db_module.upsert_gallery_item(
        "tag-a", _custom_result(HASH_B, dpi=1200, model="ultrasharp_v2"), db_path=db_path
    )
    items = list_gallery_items("tag-a", db_path=db_path)
    assert {i["custom_hash"] for i in items} == {HASH_A, HASH_B}
    assert all(i["scryfall_id"] is None for i in items)

    # Same variant again refreshes in place rather than inserting a second.
    db_module.upsert_gallery_item(
        "tag-a", _custom_result(HASH_A, dpi=1200, model="ultrasharp_v2"), db_path=db_path
    )
    assert len(list_gallery_items("tag-a", db_path=db_path)) == 2


def test_find_generated_image_looks_up_by_identity(db_path: Path) -> None:
    db_module.upsert_gallery_item(
        "tag-a", _custom_result(HASH_A, dpi=1200, model="ultrasharp_v2"), db_path=db_path
    )
    found = db_module.find_generated_image(
        f"custom:{HASH_A}", None, "ultrasharp_v2", 1200, db_path=db_path
    )
    assert found is not None and found["custom_hash"] == HASH_A
    assert (
        db_module.find_generated_image(
            f"custom:{HASH_B}", None, "ultrasharp_v2", 1200, db_path=db_path
        )
        is None
    )


# --------------------------------------------------------------------
# Filenames and cache paths
# --------------------------------------------------------------------


def test_output_filename_round_trips_through_the_parser() -> None:
    name = output_filename(
        "My Alter", "", "", None, "ultrasharp_v2", 1200, custom_hash=HASH_A
    )
    assert name == f"My_Alter-custom-{HASH_A}-ultrasharp_v2-1200dpi.png"

    parsed = parse_output_filename(name)
    assert parsed is not None
    assert parsed["custom_hash"] == HASH_A
    assert parsed["model"] == "ultrasharp_v2"
    assert parsed["dpi"] == 1200
    # No printing is invented for it: match_quantities matches on
    # (set_code, collector_number), so a placeholder would make every
    # custom card collide into one print unit.
    assert parsed["set_code"] == ""
    assert parsed["collector_number"] == ""
    assert parsed["scryfall_id"] == ""


def test_scryfall_filenames_still_parse_with_no_custom_hash() -> None:
    parsed = parse_output_filename("Sol_Ring-C21-263-ultrasharp_v2-1200dpi.png")
    assert parsed is not None
    assert parsed["custom_hash"] is None
    assert parsed["set_code"] == "c21"


def test_cache_paths_are_filename_safe_and_distinct(tmp_path: Path) -> None:
    """'custom:<hash>' is the identity, but a colon is not a legal Windows
    filename character — the cache uses an underscore form instead."""
    orig = original_cache_path(tmp_path, None, None, custom_hash=HASH_A)
    upscaled = cache_path(tmp_path, None, None, 4, "ultrasharp_v2", custom_hash=HASH_A)
    for p in (orig, upscaled):
        assert ":" not in p.name
        assert HASH_A in p.name
    assert orig != cache_path(tmp_path, None, None, 4, "ultrasharp_v2", custom_hash=HASH_B)


def test_face_group_key_does_not_collapse_distinct_customs() -> None:
    """Custom images have no set_code/collector_number, so without an
    explicit branch they would all share the 'unknown' key — and one image
    would print in place of every custom card in the project."""
    a = _custom_result(HASH_A, dpi=1200, model="ultrasharp_v2")
    b = _custom_result(HASH_B, dpi=1200, model="ultrasharp_v2")
    assert face_group_key(a) != face_group_key(b)
    # Stable across variants of the same image.
    assert face_group_key(a) == face_group_key(
        _custom_result(HASH_A, dpi=600, model="realesrgan_anime_fast")
    )


# --------------------------------------------------------------------
# Printing: the two pdf_layout rules
# --------------------------------------------------------------------


def _custom_entry(custom_hash: str, quantity: int = 1) -> DeckEntry:
    return DeckEntry(quantity=quantity, name="My Alter", custom_hash=custom_hash)


def test_unupscaled_custom_prints_alongside_upscaled_scryfall_cards() -> None:
    """The core rule. use_originals is an exclusive world for Scryfall
    cards, but a custom source has no download world to fall back to — it
    is the only image that exists until the user upscales it, so it must
    stay printable in an upscale run."""
    gallery = [_custom_result(HASH_A, dpi=431, model=CUSTOM_SOURCE_MODEL)]
    units, missing, missing_at_dpi = match_quantities(
        [_custom_entry(HASH_A, quantity=3)], gallery, use_originals=False
    )
    assert len(units) == 1
    assert units[0].quantity == 3
    assert missing == [] and missing_at_dpi == []


def test_preferred_dpi_does_not_blank_an_unupscaled_custom() -> None:
    """preferred_dpi is a hard filter for Scryfall cards — a face without
    that DPI means "never generated", worth reporting. A custom the user
    chose not to upscale has exactly one image in existence; dropping it
    prints a hole where the card they supplied should be."""
    gallery = [_custom_result(HASH_A, dpi=431, model=CUSTOM_SOURCE_MODEL)]
    units, missing, missing_at_dpi = match_quantities(
        [_custom_entry(HASH_A)], gallery, preferred_dpi=1200
    )
    assert len(units) == 1
    assert units[0].best.dpi == 431
    assert units[0].dpi_fallback is True
    assert missing_at_dpi == []


def test_preferred_dpi_still_wins_when_the_custom_was_upscaled() -> None:
    gallery = [
        _custom_result(HASH_A, dpi=431, model=CUSTOM_SOURCE_MODEL),
        _custom_result(HASH_A, dpi=1200, model="ultrasharp_v2"),
    ]
    units, _, missing_at_dpi = match_quantities(
        [_custom_entry(HASH_A)], gallery, preferred_dpi=1200
    )
    assert len(units) == 1
    assert units[0].best.dpi == 1200
    assert units[0].dpi_fallback is False
    assert missing_at_dpi == []


def test_use_originals_narrows_customs_to_their_uploaded_source() -> None:
    gallery = [
        _custom_result(HASH_A, dpi=431, model=CUSTOM_SOURCE_MODEL),
        _custom_result(HASH_A, dpi=1200, model="ultrasharp_v2"),
    ]
    units, _, _ = match_quantities([_custom_entry(HASH_A)], gallery, use_originals=True)
    assert len(units) == 1
    assert units[0].best.model == CUSTOM_SOURCE_MODEL


def test_scryfall_preferred_dpi_filter_is_unchanged() -> None:
    """The custom exemption must not have loosened the rule it sits beside."""
    scryfall = FaceResult(
        out_path=Path("/out/sol.png"),
        original_path=Path("/cache/sol.png"),
        scryfall_id="sol-id",
        face_index=None,
        face_name="Sol Ring",
        card_name="Sol Ring",
        set_code="c21",
        collector_number="263",
        png_url="https://example.com/sol.png",
        dpi=600,
        model="ultrasharp_v2",
        total_faces=1,
    )
    entry = DeckEntry(quantity=1, name="Sol Ring", set_code="c21", collector_number="263")
    units, _, missing_at_dpi = match_quantities([entry], [scryfall], preferred_dpi=1200)
    assert units == []
    assert len(missing_at_dpi) == 1


def test_custom_and_scryfall_entries_never_match_each_other() -> None:
    """Matching a custom by name would let a custom front called "Sol Ring"
    soak up a real Sol Ring line's quantity, and vice versa."""
    custom = _custom_result(HASH_A, dpi=431, model=CUSTOM_SOURCE_MODEL, card_name="Sol Ring")
    scryfall_entry = DeckEntry(quantity=4, name="Sol Ring")
    units, missing, _ = match_quantities([scryfall_entry], [custom])
    assert units == []
    assert len(missing) == 1


# --------------------------------------------------------------------
# Migration 008
# --------------------------------------------------------------------

_V7_SCHEMA = """
CREATE TABLE generation_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_tag TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    scryfall_id TEXT NOT NULL,
    face_index INTEGER,
    face_label TEXT,
    face_name TEXT NOT NULL,
    card_name TEXT NOT NULL,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    png_url TEXT NOT NULL,
    dpi INTEGER NOT NULL,
    model TEXT NOT NULL,
    tile_size INTEGER NOT NULL DEFAULT 0,
    output_dir TEXT NOT NULL,
    cache_dir TEXT NOT NULL,
    weights_dir TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    total_faces INTEGER,
    lang TEXT NOT NULL DEFAULT 'en',
    force INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE generated_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id TEXT NOT NULL,
    face_index INTEGER,
    face_name TEXT,
    card_name TEXT,
    set_code TEXT,
    collector_number TEXT,
    face_label TEXT,
    model TEXT NOT NULL,
    dpi INTEGER NOT NULL,
    native_scale INTEGER NOT NULL DEFAULT 4,
    device TEXT NOT NULL DEFAULT 'unknown',
    image_filename TEXT NOT NULL,
    out_path TEXT NOT NULL,
    original_path TEXT NOT NULL,
    png_url TEXT NOT NULL,
    created_at TEXT,
    total_faces INTEGER,
    lang TEXT NOT NULL DEFAULT 'en'
);
CREATE UNIQUE INDEX idx_generated_images_variant
    ON generated_images(scryfall_id, COALESCE(face_index, -1), model, dpi);
CREATE TABLE project_gallery_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_tag TEXT NOT NULL,
    image_id INTEGER NOT NULL
        REFERENCES generated_images(id) ON DELETE CASCADE,
    UNIQUE (project_tag, image_id)
);
CREATE TABLE worker_control (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _make_v7_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V7_SCHEMA)
        conn.execute(
            "INSERT INTO generation_tasks (project_tag, scryfall_id, face_name, "
            "card_name, set_code, collector_number, png_url, dpi, model, output_dir, "
            "cache_dir, weights_dir, created_at) VALUES "
            "('tag-a','sol-id','Sol Ring','Sol Ring','c21','263',"
            "'https://example.com/sol.png',1200,'ultrasharp_v2','/o','/c','/w','2024-01-01')"
        )
        for i, sid in enumerate(["sol-id", "bolt-id"]):
            conn.execute(
                "INSERT INTO generated_images (scryfall_id, model, dpi, image_filename, "
                "out_path, original_path, png_url, created_at) VALUES "
                f"('{sid}','ultrasharp_v2',1200,'f{i}.png','/o/f{i}.png','/c/o{i}.png','u',"
                "'2024-01-01')"
            )
        conn.executescript(
            "INSERT INTO project_gallery_memberships (project_tag, image_id) VALUES"
            " ('tag-a', 1), ('tag-a', 2), ('tag-b', 1);"
        )
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()


def test_migration_008_preserves_gallery_memberships(tmp_path: Path) -> None:
    """The regression this migration nearly shipped.

    generated_images is rebuilt, and with foreign_keys=ON an ALTER TABLE
    RENAME rewrites REFERENCES clauses in *other* tables to follow the new
    name — a behaviour PRAGMA legacy_alter_table does NOT switch off. So
    renaming it out of the way silently repoints memberships at the old
    table, and dropping that then cascade-deletes every membership: every
    project's gallery emptied, with the migration reporting success.
    """
    path = tmp_path / "v7.db"
    _make_v7_db(path)

    init_db(path)

    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert conn.execute(
            "SELECT COUNT(*) FROM project_gallery_memberships"
        ).fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM generated_images").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 1
        # Memberships must reference the rebuilt table, by the same ids.
        assert [r[2] for r in conn.execute(
            "PRAGMA foreign_key_list(project_gallery_memberships)"
        )] == ["generated_images"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert sorted(
            conn.execute(
                "SELECT project_tag, image_id FROM project_gallery_memberships"
            )
        ) == [("tag-a", 1), ("tag-a", 2), ("tag-b", 1)]
    finally:
        conn.close()


def test_migration_008_leaves_existing_rows_scryfall_identified(tmp_path: Path) -> None:
    path = tmp_path / "v7.db"
    _make_v7_db(path)
    init_db(path)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        for table in ("generation_tasks", "generated_images"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            assert rows
            assert all(r["custom_hash"] is None for r in rows)
            assert all(r["scryfall_id"] for r in rows)
    finally:
        conn.close()


def test_migration_008_enforces_one_identity_per_row(tmp_path: Path) -> None:
    path = tmp_path / "v7.db"
    _make_v7_db(path)
    init_db(path)

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    base = (
        "INSERT INTO generated_images (scryfall_id, custom_hash, model, dpi, "
        "image_filename, out_path, original_path) VALUES (?, ?, 'm', 1, 'f', 'o', 'p')"
    )
    try:
        for scryfall_id, custom_hash in [(None, None), ("x", HASH_A)]:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(base, (scryfall_id, custom_hash))
        # And the uniqueness backstop must still cover custom rows, which
        # it only does because the index is built over the identity
        # expression rather than the (now nullable) scryfall_id column.
        conn.execute(base, (None, HASH_A))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (None, HASH_A))
    finally:
        conn.close()


# --------------------------------------------------------------------
# End to end through the worker's task path
# --------------------------------------------------------------------


def test_custom_source_task_registers_the_upload_as_printable(
    tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    """The path that makes an un-upscaled custom printable: no network, no
    upscaler, just the uploaded file registered as its own variant."""
    from proxy_scaler import pipeline
    from proxy_scaler.services import generation as gen

    # customs.CUSTOMS_DIR_NAME is a relative name resolved against the
    # server's cwd (and frozen as a default argument), so redirecting it
    # means moving the cwd — exactly what the real server relies on.
    monkeypatch.chdir(tmp_path)
    content_hash, _ = customs.store_original(_png_bytes(745, 1040))

    entry = DeckEntry(quantity=2, name="My Alter", custom_hash=content_hash)
    queued, failed, task_ids = gen.enqueue_download_entries(
        [entry],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag-a",
        db_path=db_path,
    )
    assert (queued, failed) == (1, 0)

    task = db_module.get_task(task_ids[0], db_path=db_path)
    assert task.model == CUSTOM_SOURCE_MODEL
    assert task.custom_hash == content_hash
    # The DPI was measured at enqueue time and frozen onto the row, so the
    # variant key can't drift between enqueue, skip-check and the worker.
    assert task.dpi == pytest.approx(300, abs=1)

    result = pipeline.process_custom_source_task(task)
    assert result.out_path == result.original_path
    assert result.out_path.is_file()
    assert result.dpi == task.dpi
    assert result.identity_key == f"custom:{content_hash}"

    db_module.upsert_gallery_item_for_task(task, result, db_path=db_path)
    items = list_gallery_items("tag-a", db_path=db_path)
    assert len(items) == 1 and items[0]["custom_hash"] == content_hash

    # And it prints, in a normal (non-originals) run.
    gallery = [FaceResult.from_dict(i) for i in items]
    units, missing, missing_at_dpi = match_quantities([entry], gallery)
    assert len(units) == 1 and units[0].quantity == 2
    assert missing == [] and missing_at_dpi == []


def test_enqueue_reports_a_custom_that_was_never_uploaded(
    tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    """Enqueue has to measure the file to fix the variant's DPI, so a
    missing upload is a per-entry failure with a real message — not a
    crash, and not a task that would fail later in the worker."""
    from proxy_scaler.services import generation as gen

    monkeypatch.chdir(tmp_path)
    notes: list[str] = []
    queued, failed, task_ids = gen.enqueue_download_entries(
        [DeckEntry(quantity=1, name="Missing", custom_hash=HASH_A)],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag-a",
        on_note=notes.append,
        db_path=db_path,
    )
    assert (queued, failed, task_ids) == (0, 1, [])
    assert any("has not been uploaded" in n for n in notes)


def test_custom_entries_never_reach_the_card_resolver(
    tmp_path: Path, db_path: Path, monkeypatch
) -> None:
    """A Custom Image is not a Scryfall card. Sending it to the resolver
    would at best waste a lookup and at worst match some unrelated real
    card by name."""
    from proxy_scaler.card_lookup import CardResolver
    from proxy_scaler.services import generation as gen

    monkeypatch.chdir(tmp_path)
    customs.store_original(_png_bytes(745, 1040))

    seen: list[list] = []

    def _spy(self, entries):
        seen.append(list(entries))
        return []

    monkeypatch.setattr(CardResolver, "resolve_many", _spy)
    gen.enqueue_decklist_entries(
        [DeckEntry(quantity=1, name="Sol Ring", custom_hash=HASH_A)],
        model="ultrasharp_v2",
        dpi_targets=[1200],
        skip_existing=True,
        tile_size=0,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        weights_dir=tmp_path / "weights",
        project_tag="tag-a",
        db_path=db_path,
    )
    # Not even called with an empty list — a batch of only custom entries
    # skips resolution entirely.
    assert seen == []
