"""Shared decklist → Scryfall → upscale pipeline for CLI and UI."""

from __future__ import annotations

import contextlib
import io
import re
import shutil
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from .db import TaskRow

from .decklist import DeckEntry
from .dpi import (
    DEFAULT_DPI,
    ORIGINAL_DPI,
    ORIGINAL_MODEL,
    native_scale_for_dpi,
    resolve_dpi_targets,
    target_pixels,
)
from .scryfall import (
    CardFaceImage,
    ScryfallClient,
    ScryfallError,
    download_png,
    expand_faces,
)
from .upscale import (
    UpscaleModel,
    Upscaler,
    cache_path,
    effective_tile_size,
    load_or_upscale,
    original_cache_path,
    original_thumb_path,
    parse_model,
    read_cache_device,
)

ProgressCallback = Callable[[str], None]


def _safe_filename_part(text: str) -> str:
    text = text.replace("//", "&")
    text = text.replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "card"


def output_filename(
    face_name: str,
    set_code: str,
    collector: str,
    face_label: str | None,
    model: UpscaleModel | str | None = None,
    dpi: int | None = None,
    *,
    lang: str | None = None,
    scryfall_id: str | None = None,
) -> str:
    """Name-SET-COLLECTOR[-lang][-face]-model-dpi[-scryfall_id].png. The
    lang segment is lowercase and only written for non-English printings —
    English output keeps the exact pre-language filename shape, so nothing
    regenerates just because this field appeared (and
    db.parse_output_filename reads a missing segment back as "en" for the
    same reason). The trailing scryfall_id (written for every new file)
    makes a file recoverable into the generated_images registry from its
    name alone — no card-corpus lookup needed; files named before it
    existed are never renamed, so consumers probe both shapes (see
    db._OUTPUT_SUFFIX_RE and the skip-existing check in
    services/generation.py)."""
    base = (
        f"{_safe_filename_part(face_name)}-"
        f"{set_code.upper()}-"
        f"{_safe_filename_part(collector)}"
    )
    if lang and lang != "en":
        base = f"{base}-{lang.lower()}"
    if face_label:
        base = f"{base}-{face_label}"
    if model is not None:
        base = f"{base}-{parse_model(model).value}"
    if dpi is not None:
        base = f"{base}-{dpi}dpi"
    if scryfall_id:
        base = f"{base}-{scryfall_id.lower()}"
    return f"{base}.png"


@dataclass
class FaceResult:
    """One written face with paths needed for comparison / regenerate."""

    out_path: Path
    original_path: Path
    scryfall_id: str
    face_index: int | None
    face_name: str
    card_name: str
    set_code: str
    collector_number: str
    png_url: str
    dpi: int
    model: str = UpscaleModel.ULTRASHARP_V2.value
    face_label: str | None = None
    native_scale: int = 4
    device: str = "unknown"  # "gpu" | "cpu" | "unknown"
    # Scryfall language code of the printing (see scryfall.CardFaceImage).
    # "en" for results predating db migration 005.
    lang: str = "en"
    # ISO-8601 UTC, from generated_images.created_at — when this image
    # was last produced. None for a freshly-built result that hasn't been
    # persisted yet, and for gallery rows predating db migration 002.
    # pdf_layout._pick_dpi_variant treats None as older than any timestamp.
    created_at: str | None = None
    # How many physical faces this card has (see scryfall.CardFaceImage).
    # None for gallery rows predating db migration 003 — pdf_layout.
    # match_quantities treats that the same as "unknown, don't verify".
    total_faces: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["out_path"] = str(self.out_path)
        d["original_path"] = str(self.original_path)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> FaceResult:
        # Back-compat: older sessions stored `scale` (2/4) instead of `dpi`
        if "dpi" in data:
            dpi = int(data["dpi"])
        elif "scale" in data:
            dpi = 600 if int(data["scale"]) == 2 else 1200
        else:
            dpi = DEFAULT_DPI
        device = str(data.get("device") or "unknown").lower()
        if device not in ("gpu", "cpu", "unknown"):
            device = "unknown"
        return cls(
            out_path=Path(data["out_path"]),
            original_path=Path(data["original_path"]),
            scryfall_id=data["scryfall_id"],
            face_index=data["face_index"],
            face_name=data["face_name"],
            card_name=data["card_name"],
            set_code=data["set_code"],
            collector_number=data["collector_number"],
            png_url=data["png_url"],
            dpi=dpi,
            model=data.get("model", UpscaleModel.ULTRASHARP_V2.value),
            face_label=data.get("face_label"),
            native_scale=int(data.get("native_scale", data.get("scale", 4))),
            device=device,
            created_at=data.get("created_at"),
            total_faces=data.get("total_faces"),
            lang=data.get("lang") or "en",
        )


def face_group_key(item: FaceResult) -> str:
    """Stable identity for grouping/tracking one card face across DPIs and
    models. Prefers set_code+collector_number — a stable physical-printing
    key — over scryfall_id. Items reconstructed from disk
    (db.py::scan_gallery_from_output) never have a real scryfall_id, so
    keying on it first would silently split a freshly-generated variant and
    a disk-recovered variant of the exact same card into two separate
    groups — and, via pdf_layout.py's match_quantities(), into two
    duplicate physical print slots for the same card.
    """
    if item.set_code and item.collector_number:
        # Language is part of the printing identity (Italian and English
        # rows of one set/collector are different cards); absent lang
        # normalizes to "en" so pre-language rows keep matching.
        identity = (
            f"{item.set_code.lower()}/{item.collector_number}/"
            f"{(item.lang or 'en').lower()}"
        )
    else:
        identity = item.scryfall_id or "unknown"
    return f"{identity}:{item.face_index}:{item.face_label}"


def group_by_face(items: list[FaceResult]) -> list[tuple[str, list[FaceResult]]]:
    """Group results by card face (same printing/face) for multi-DPI/model rows."""
    groups: dict[str, list[FaceResult]] = defaultdict(list)
    order: list[str] = []
    for item in items:
        key = face_group_key(item)
        if key not in groups:
            order.append(key)
        groups[key].append(item)
    result: list[tuple[str, list[FaceResult]]] = []
    for key in order:
        face_items = sorted(groups[key], key=lambda x: (x.dpi, x.model))
        result.append((key, face_items))
    return result


FaceDoneCallback = Callable[[FaceResult], None]


@dataclass
class PipelineResult:
    wrote: list[FaceResult] = field(default_factory=list)
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def clear_generated_data(
    *dirs: Path,
) -> list[str]:
    """Delete the contents of generated output/cache directories, keeping
    the directories themselves. Returns human-readable notes.

    The roots must stay alive: they sit inside the repo, and uvicorn's
    --reload stat-watcher rglobs the reload dir every second — a watched
    directory disappearing mid-scan raises FileNotFoundError and takes the
    whole dev server down."""
    notes: list[str] = []
    for path in dirs:
        path = Path(path)
        if not path.exists():
            notes.append(f"skipped missing: {path}")
            continue
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        notes.append(f"cleared: {path}")
    return notes


def _save_original(
    png_bytes: bytes,
    cache_dir: Path,
    scryfall_id: str,
    face_index: int | None,
    *,
    overwrite: bool = False,
) -> Path:
    path = original_cache_path(cache_dir, scryfall_id, face_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not path.exists():
        path.write_bytes(png_bytes)
    return path


_THUMB_MAX_DIM = 220  # px, longest side — does most of the size-reduction work
_THUMB_TARGET_BYTES = 50_000
_THUMB_QUALITY_STEPS = (85, 70, 55, 40)  # last is the floor, always accepted


def _generate_original_thumbnail(original_path: Path, thumb_path: Path) -> None:
    """Small JPEG preview thumbnail from a cached original PNG, for the PDF
    layout preview grid (pdf.py::preview_page) — not print-quality.

    Corner-flattened the same way build_pdf() flattens its full-size
    images, and in the same order (BEFORE resizing — resizing while the
    corners are still transparent lets LANCZOS blend transparent RGB into
    the opaque body at the boundary, baking in a smear). The preview grid
    composites these thumbnails edge-to-edge against their neighbours, so
    without this the raw rounded-corner transparency flattens to a white
    notch at every corner, visible as a light cross at each 4-card
    junction — this used to be skipped on the theory that the tile was
    shown standalone, but preview_page() is its only caller and it never
    is. Deferred import: pdf_layout imports this module, so importing it
    back at module scope here would be circular."""
    from .pdf_layout import flatten_corner_alpha

    with Image.open(original_path) as raw:
        img = flatten_corner_alpha(raw.convert("RGBA"))
        w, h = img.size
        scale = _THUMB_MAX_DIM / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])

    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    for quality in _THUMB_QUALITY_STEPS:
        buf = io.BytesIO()
        bg.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= _THUMB_TARGET_BYTES or quality == _THUMB_QUALITY_STEPS[-1]:
            thumb_path.write_bytes(data)
            return


def ensure_original_thumbnail(original_path: Path) -> Path | None:
    """Self-healing: return the thumbnail path for `original_path`,
    generating it on demand if missing — covers both a fresh download (see
    the eager call in _regenerate_face_from_card below) and an original
    that predates this feature or was cached via the skip_existing fast
    path in services/generation.py (which stores original_path without
    ever touching the file). Returns None if `original_path` itself isn't
    on disk — mirrors gallery.py's _resolve_existing tolerant-missing-file
    idiom; the caller treats this as a missing/blank slot, not a crash."""
    if not original_path.is_file():
        return None
    thumb_path = original_thumb_path(original_path)
    if not thumb_path.is_file():
        try:
            _generate_original_thumbnail(original_path, thumb_path)
        except Exception as exc:  # noqa: BLE001 — best-effort, never break real generation
            print(f"Failed to generate thumbnail for {original_path}: {exc}", file=sys.stderr)
            return None
    return thumb_path


def _resize_to_dpi(image: Image.Image, dpi: int) -> Image.Image:
    target = target_pixels(dpi)
    if image.size == target:
        return image
    return image.resize(target, Image.Resampling.LANCZOS)


def _write_dpi_variant(
    *,
    face: CardFaceImage,
    raw: Image.Image,
    original_path: Path,
    output_dir: Path,
    model_id: UpscaleModel,
    dpi: int,
    native_scale: int,
    device: str = "unknown",
) -> FaceResult:
    out_name = output_filename(
        face.face_name,
        face.set_code,
        face.collector_number,
        face.face_label,
        model_id,
        dpi,
        lang=face.lang,
        scryfall_id=face.scryfall_id,
    )
    out_path = output_dir / out_name
    output_dir.mkdir(parents=True, exist_ok=True)
    # Deliberately no corner-flatten/bleed here. The generated PNG is a
    # faithful upscale of the source art and keeps its transparent rounded
    # corners; flattening them to opaque is a *printing* concern (physical
    # proxies print a full rectangle and get punched round afterwards) and
    # belongs only in the PDF pipelines, which apply it per export via
    # pdf_layout.flatten_corner_alpha/add_bleed. Doing it here once baked
    # a visible replicated-pixel smear into every corner of the saved file
    # -- irreversible, and wrong for anyone downloading the PNG directly.
    sized = _resize_to_dpi(raw, dpi)
    sized.save(out_path, format="PNG")
    return FaceResult(
        out_path=out_path,
        original_path=original_path,
        scryfall_id=face.scryfall_id,
        face_index=face.face_index,
        face_name=face.face_name,
        card_name=face.card_name,
        set_code=face.set_code,
        collector_number=face.collector_number,
        png_url=face.png_url,
        dpi=dpi,
        model=model_id.value,
        face_label=face.face_label,
        total_faces=face.total_faces,
        native_scale=native_scale,
        device=device,
        lang=face.lang,
    )


def _phase(timings: object | None, name: str):
    """Enter a phase on a duck-typed timing_db.TimingCollector, or no-op.

    Kept duck-typed so this module never imports timing_db (which imports
    db, which imports upscale — a cycle otherwise)."""
    if timings is not None:
        return timings.phase(name)
    return contextlib.nullcontext()


def _upscalers_for_targets(
    model_id: UpscaleModel,
    dpi_targets: list[int],
    weights_dir: Path,
    tile_size: int = 0,
    timings: object | None = None,
) -> dict[int, Upscaler]:
    """Build unique Upscaler instances keyed by native scale.

    tile_size is the raw client value (0 = "auto") — resolved here via
    effective_tile_size() rather than passed straight through, so a
    memory-hungry model (UltraSharpV2, IllustrationJaNai) still gets
    tiled by default even when nothing explicit was set. This is the one
    choke point every generation path (process_entries,
    _regenerate_face_from_card/process_task, regenerate_face_multi) funnels
    through, so resolving it here covers all of them."""
    needed = {native_scale_for_dpi(d, model_id) for d in dpi_targets}
    tile = effective_tile_size(model_id, tile_size)
    return {
        scale: Upscaler(
            model=model_id,
            scale=scale,
            weights_dir=weights_dir,
            tile=tile,
            timings=timings,
            tile_auto=tile_size == 0,
        )
        for scale in sorted(needed)
    }


def _regenerate_face_from_card(
    face: CardFaceImage,
    *,
    dpi_targets: list[int],
    output_dir: Path,
    cache_dir: Path,
    weights_dir: Path,
    model_id: UpscaleModel,
    tile_size: int = 0,
    known_original_path: Path | None = None,
    on_progress: ProgressCallback | None = None,
    timings: object | None = None,
) -> list[FaceResult]:
    """Shared core of regenerate_face_multi()/process_task(): given an
    already-resolved CardFaceImage (no Scryfall call needed here — the
    caller already has scryfall_id/png_url/etc.), reuse a cached original
    if present (downloading via png_url only if not), then force-upscale
    at each requested DPI, sharing one upscale pass per distinct native
    scale — e.g. 800 and 1200 DPI both resolving to native x4 only run the
    model once.

    `known_original_path`, when given (regenerate_face_multi's caller
    already has a FaceResult with its own recorded original_path), is
    checked first; the canonical cache_dir-derived location is always
    checked as a fallback (the only option process_task has, since a task
    row has no prior FaceResult to carry a known path)."""
    if not dpi_targets:
        return []

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    upscalers = _upscalers_for_targets(
        model_id, dpi_targets, Path(weights_dir), tile_size, timings=timings
    )

    canonical_original = original_cache_path(cache_dir, face.scryfall_id, face.face_index)
    cached_original = next(
        (
            p
            for p in (known_original_path, canonical_original)
            if p is not None and p.is_file()
        ),
        None,
    )
    if cached_original is not None:
        png_bytes = cached_original.read_bytes()
        dpi_label = ",".join(str(d) for d in sorted(set(dpi_targets)))
        log(
            f"Regenerating {dpi_label} DPI with {model_id.value} "
            f"from {cached_original.name}"
        )
    else:
        log(f"Downloading {face.png_url}")
        with _phase(timings, "download"):
            png_bytes = download_png(face.png_url)

    original_path = _save_original(
        png_bytes, cache_dir, face.scryfall_id, face.face_index
    )
    ensure_original_thumbnail(original_path)  # best-effort; fails soft internally

    raw_by_scale: dict[int, Image.Image] = {}
    device_by_scale: dict[int, str] = {}
    results: list[FaceResult] = []
    for target_dpi in sorted(set(dpi_targets)):
        native = native_scale_for_dpi(target_dpi, model_id)
        if native not in raw_by_scale:
            upscaled = load_or_upscale(
                png_bytes=png_bytes,
                upscaler=upscalers[native],
                cache_dir=cache_dir,
                scryfall_id=face.scryfall_id,
                face_index=face.face_index,
                force=True,
                timings=timings,
            )
            raw_by_scale[native] = upscaled.image
            device_by_scale[native] = upscaled.device
            if timings is not None:
                timings.set_device(upscaled.device)
                timings.set_dtype(getattr(upscaled, "dtype", None))

        with _phase(timings, "encode"):
            result = _write_dpi_variant(
                face=face,
                raw=raw_by_scale[native],
                original_path=original_path,
                output_dir=output_dir,
                model_id=model_id,
                dpi=target_dpi,
                native_scale=native,
                device=device_by_scale[native],
            )
        log(f"  regenerated {result.out_path} ({result.out_path.stat().st_size} bytes)")
        results.append(result)

    return results


def regenerate_face_multi(
    item: FaceResult,
    *,
    dpi_targets: list[int],
    output_dir: Path | None = None,
    cache_dir: Path = Path("imgcache"),
    weights_dir: Path = Path("weights"),
    model: UpscaleModel | str | None = None,
    tile_size: int = 0,
    on_progress: ProgressCallback | None = None,
) -> list[FaceResult]:
    """Force re-upscale one face at multiple target DPIs (bypasses upscale
    cache), reusing a single upscale pass per distinct native scale — e.g.
    800 and 1200 DPI both resolving to native x4 only run the model once."""
    model_id = parse_model(model) if model is not None else parse_model(item.model)
    output_dir = output_dir or item.out_path.parent
    face = CardFaceImage(
        scryfall_id=item.scryfall_id,
        card_name=item.card_name,
        face_name=item.face_name,
        set_code=item.set_code,
        collector_number=item.collector_number,
        png_url=item.png_url,
        face_index=item.face_index,
        total_faces=item.total_faces,
        lang=item.lang,
    )
    return _regenerate_face_from_card(
        face,
        dpi_targets=dpi_targets,
        output_dir=output_dir,
        cache_dir=cache_dir,
        weights_dir=weights_dir,
        model_id=model_id,
        tile_size=tile_size,
        known_original_path=item.original_path,
        on_progress=on_progress,
    )


def process_download_task(
    task: TaskRow,
    *,
    on_progress: ProgressCallback | None = None,
    timings: object | None = None,
) -> FaceResult:
    """Process one download task (model == ORIGINAL_MODEL): fetch the
    Scryfall original and cache it, no upscaling. Always overwrites the
    cached original — skip-existing economy lives at enqueue time (see
    services/generation.py::enqueue_download_entries), which is what makes
    Re-Fetch just "enqueue a download task" with no delete-then-download
    race against _save_original's default write-if-missing behavior. The
    resulting FaceResult points out_path at the original itself, so the
    gallery row's out_path and original_path coincide."""
    if on_progress:
        on_progress(f"Downloading original for {task.face_name}…")
    with _phase(timings, "download"):
        png_bytes = download_png(task.png_url)
    with _phase(timings, "encode"):
        original_path = _save_original(
            png_bytes,
            Path(task.cache_dir),
            task.scryfall_id,
            task.face_index,
            overwrite=True,
        )
        # The thumbnail is derived from the original, so a re-fetch must
        # invalidate it — delete before the ensure call regenerates it.
        thumb = original_thumb_path(original_path)
        thumb.unlink(missing_ok=True)
        ensure_original_thumbnail(original_path)
    return FaceResult(
        out_path=original_path,
        original_path=original_path,
        scryfall_id=task.scryfall_id,
        face_index=task.face_index,
        face_name=task.face_name,
        card_name=task.card_name,
        set_code=task.set_code,
        collector_number=task.collector_number,
        png_url=task.png_url,
        dpi=ORIGINAL_DPI,
        model=ORIGINAL_MODEL,
        face_label=task.face_label,
        native_scale=1,
        total_faces=task.total_faces,
        lang=task.lang,
    )


def process_task(
    task: TaskRow,
    *,
    on_progress: ProgressCallback | None = None,
    timings: object | None = None,
) -> FaceResult:
    """Process one generation_tasks row (see db.py) into a FaceResult —
    the unit of work the background worker (worker.py) performs. Shares
    its core with regenerate_face_multi(), just fed from a task row's
    already-resolved fields (set at enqueue time, no Scryfall call needed
    here) instead of an existing FaceResult, and always exactly one target
    DPI (one task = one face+dpi+model unit of work)."""
    if task.model == ORIGINAL_MODEL:
        # Must branch before parse_model — the sentinel isn't an UpscaleModel.
        return process_download_task(task, on_progress=on_progress, timings=timings)
    model_id = parse_model(task.model)
    face = CardFaceImage(
        scryfall_id=task.scryfall_id,
        card_name=task.card_name,
        face_name=task.face_name,
        set_code=task.set_code,
        collector_number=task.collector_number,
        png_url=task.png_url,
        face_index=task.face_index,
        total_faces=task.total_faces,
        lang=task.lang,
    )
    results = _regenerate_face_from_card(
        face,
        dpi_targets=[task.dpi],
        output_dir=Path(task.output_dir),
        cache_dir=Path(task.cache_dir),
        weights_dir=Path(task.weights_dir),
        model_id=model_id,
        tile_size=task.tile_size,
        on_progress=on_progress,
        timings=timings,
    )
    return results[0]


def expected_face_result(task: TaskRow) -> FaceResult:
    """Deterministically reconstruct the FaceResult a *done* task produced,
    purely from the task row's own fields — no DB gallery lookup needed.
    Must not be fed download tasks (model == ORIGINAL_MODEL) — parse_model
    below raises on the sentinel.
    Lets the UI sync a task it enqueued into st.session_state.gallery the
    moment it's done, even when no project has been saved yet (so there's
    no project_id for the worker to attach a DB gallery row to). Mirrors
    exactly how process_entries()'s skip_existing branch builds a
    FaceResult from cache without re-running the upscaler."""
    model_id = parse_model(task.model)
    native = native_scale_for_dpi(task.dpi, model_id)
    out_path = Path(task.output_dir) / output_filename(
        task.face_name,
        task.set_code,
        task.collector_number,
        task.face_label,
        model_id,
        task.dpi,
        lang=task.lang,
        scryfall_id=task.scryfall_id,
    )
    original_path = original_cache_path(
        Path(task.cache_dir), task.scryfall_id, task.face_index
    )
    cached = cache_path(
        Path(task.cache_dir), task.scryfall_id, task.face_index, native, model_id
    )
    return FaceResult(
        out_path=out_path,
        original_path=original_path,
        scryfall_id=task.scryfall_id,
        face_index=task.face_index,
        face_name=task.face_name,
        card_name=task.card_name,
        set_code=task.set_code,
        collector_number=task.collector_number,
        png_url=task.png_url,
        dpi=task.dpi,
        model=task.model,
        face_label=task.face_label,
        total_faces=task.total_faces,
        native_scale=native,
        device=read_cache_device(cached),
        lang=task.lang,
    )


def regenerate_face(
    item: FaceResult,
    *,
    output_dir: Path | None = None,
    cache_dir: Path = Path("imgcache"),
    weights_dir: Path = Path("weights"),
    dpi: int | None = None,
    model: UpscaleModel | str | None = None,
    tile_size: int = 0,
    on_progress: ProgressCallback | None = None,
) -> FaceResult:
    """Force re-upscale one face at a target DPI (bypasses upscale cache)."""
    dpi = dpi if dpi is not None else item.dpi
    results = regenerate_face_multi(
        item,
        dpi_targets=[dpi],
        output_dir=output_dir,
        cache_dir=cache_dir,
        weights_dir=weights_dir,
        model=model,
        tile_size=tile_size,
        on_progress=on_progress,
    )
    return results[0]


def process_entries(
    entries: list[DeckEntry],
    *,
    output_dir: Path,
    dpi: int = DEFAULT_DPI,
    all_dpis: bool = False,
    dpi_targets: list[int] | None = None,
    model: UpscaleModel | str = UpscaleModel.ULTRASHARP_V2,
    cache_dir: Path = Path("imgcache"),
    weights_dir: Path = Path("weights"),
    skip_existing: bool = False,
    force: bool = False,
    tile_size: int = 0,
    on_progress: ProgressCallback | None = None,
    on_face_done: FaceDoneCallback | None = None,
) -> PipelineResult:
    """Resolve, download, upscale, and write PNGs for each deck entry.

    `dpi_targets`, when given, selects an arbitrary DPI subset directly
    (used by the UI's independent checkboxes) and takes priority over the
    single-`dpi`/`all_dpis` combo (used by the CLI's --dpi/--all-dpis flags).

    `tile_size` processes each face in overlapping tiles instead of one
    full-image forward pass — needed to keep memory-hungry transformer
    models (DAT-based) within a GPU's VRAM budget. 0 means "auto":
    see effective_tile_size() / _upscalers_for_targets() — off for light
    models, a sane default tile size for heavy ones. An explicit non-zero
    value always wins over that default.
    """
    result = PipelineResult()
    if not entries:
        return result

    model_id = parse_model(model)
    dpi_targets = resolve_dpi_targets(dpi=dpi, all_dpis=all_dpis, dpi_targets=dpi_targets)
    upscalers = _upscalers_for_targets(
        model_id, dpi_targets, Path(weights_dir), tile_size
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    client = ScryfallClient()
    seen_keys: set[str] = set()

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    dpi_label = ",".join(str(d) for d in dpi_targets)
    log(
        f"Processing {len(entries)} line(s) → {output_dir}/ "
        f"[{model_id.value} @ {dpi_label} DPI]"
    )

    # Resolve every entry up front in a handful of batched Scryfall
    # requests (via /cards/collection for exact printings) instead of one
    # request per card — the per-card resolve was previously the dominant
    # cost even for cards that end up skipped below.
    resolved = client.resolve_many(entries)

    for entry, pre_resolved in zip(entries, resolved):
        try:
            if isinstance(pre_resolved, ScryfallError):
                raise pre_resolved
            card, warnings = pre_resolved
            for w in warnings:
                result.notes.append(w)
                log(f"  note: {w}")

            faces = expand_faces(card)
            for face in faces:
                face_key = f"{face.scryfall_id}:{face.face_index}"
                if face_key in seen_keys:
                    log(
                        f"  skip duplicate: {face.face_name} "
                        f"({face.set_code}/{face.collector_number})"
                    )
                    result.skipped += 1
                    continue
                seen_keys.add(face_key)

                qty_note = f"qty={entry.quantity}" if entry.quantity != 1 else ""
                log(
                    f"[[{face.face_name}]] {face.card_name} "
                    f"({face.set_code}/{face.collector_number}) {qty_note}".rstrip()
                )

                png_bytes: bytes | None = None
                original_path: Path | None = None
                # Cache raw upscales by native scale for this face
                raw_by_scale: dict[int, Image.Image] = {}
                device_by_scale: dict[int, str] = {}

                for target_dpi in dpi_targets:
                    native = native_scale_for_dpi(target_dpi, model_id)
                    out_path = output_dir / output_filename(
                        face.face_name,
                        face.set_code,
                        face.collector_number,
                        face.face_label,
                        model_id,
                        target_dpi,
                        lang=face.lang,
                        scryfall_id=face.scryfall_id,
                    )
                    if not out_path.exists():
                        # Files written before the embedded-id filename
                        # format keep their legacy names forever — an
                        # existing legacy file still counts as generated.
                        legacy_path = output_dir / output_filename(
                            face.face_name,
                            face.set_code,
                            face.collector_number,
                            face.face_label,
                            model_id,
                            target_dpi,
                            lang=face.lang,
                        )
                        if legacy_path.exists():
                            out_path = legacy_path

                    if skip_existing and out_path.exists() and not force:
                        orig = original_cache_path(
                            cache_dir, face.scryfall_id, face.face_index
                        )
                        if not orig.exists():
                            if png_bytes is None:
                                png_bytes = download_png(
                                    face.png_url, session=client._session
                                )
                            orig = _save_original(
                                png_bytes,
                                cache_dir,
                                face.scryfall_id,
                                face.face_index,
                            )
                        cached = cache_path(
                            cache_dir,
                            face.scryfall_id,
                            face.face_index,
                            native,
                            model_id,
                        )
                        face_result = FaceResult(
                            out_path=out_path,
                            original_path=orig,
                            scryfall_id=face.scryfall_id,
                            face_index=face.face_index,
                            face_name=face.face_name,
                            card_name=face.card_name,
                            set_code=face.set_code,
                            collector_number=face.collector_number,
                            png_url=face.png_url,
                            dpi=target_dpi,
                            model=model_id.value,
                            face_label=face.face_label,
                            total_faces=face.total_faces,
                            native_scale=native,
                            device=read_cache_device(cached),
                            lang=face.lang,
                        )
                        log(f"  exists: {out_path.name}")
                        result.skipped += 1
                        result.wrote.append(face_result)
                        if on_face_done:
                            on_face_done(face_result)
                        continue

                    if png_bytes is None:
                        png_bytes = download_png(
                            face.png_url, session=client._session
                        )
                    if original_path is None:
                        original_path = _save_original(
                            png_bytes,
                            cache_dir,
                            face.scryfall_id,
                            face.face_index,
                        )

                    if native not in raw_by_scale:
                        upscaled = load_or_upscale(
                            png_bytes=png_bytes,
                            upscaler=upscalers[native],
                            cache_dir=cache_dir,
                            scryfall_id=face.scryfall_id,
                            face_index=face.face_index,
                            force=force,
                        )
                        raw_by_scale[native] = upscaled.image
                        device_by_scale[native] = upscaled.device

                    face_result = _write_dpi_variant(
                        face=face,
                        raw=raw_by_scale[native],
                        original_path=original_path,
                        output_dir=output_dir,
                        model_id=model_id,
                        dpi=target_dpi,
                        native_scale=native,
                        device=device_by_scale.get(native, "unknown"),
                    )
                    tw, th = target_pixels(target_dpi)
                    log(
                        f"  wrote {face_result.out_path.name} "
                        f"({tw}x{th} @ {target_dpi} DPI, {face_result.device})"
                    )
                    result.wrote.append(face_result)
                    if on_face_done:
                        on_face_done(face_result)

        except ScryfallError as exc:
            msg = f"FAIL [{entry.raw_line}]: {exc}"
            result.failed.append(msg)
            log(msg)
        except Exception as exc:  # noqa: BLE001 — keep batch going
            msg = f"FAIL [{entry.raw_line}]: {exc}"
            result.failed.append(msg)
            log(msg)

    log(
        f"Done. wrote={len(result.wrote)} skipped={result.skipped} "
        f"failed={len(result.failed)}"
    )
    return result
