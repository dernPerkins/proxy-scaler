"""Shared decklist → Scryfall → upscale pipeline for CLI and UI."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from .decklist import DeckEntry
from .dpi import (
    DEFAULT_DPI,
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
    load_or_upscale,
    original_cache_path,
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
) -> str:
    base = (
        f"{_safe_filename_part(face_name)}-"
        f"{set_code.upper()}-"
        f"{_safe_filename_part(collector)}"
    )
    if face_label:
        base = f"{base}-{face_label}"
    if model is not None:
        base = f"{base}-{parse_model(model).value}"
    if dpi is not None:
        base = f"{base}-{dpi}dpi"
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
    model: str = UpscaleModel.SWINIR.value
    face_label: str | None = None
    native_scale: int = 4
    device: str = "unknown"  # "gpu" | "cpu" | "unknown"

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
            model=data.get("model", UpscaleModel.SWINIR.value),
            face_label=data.get("face_label"),
            native_scale=int(data.get("native_scale", data.get("scale", 4))),
            device=device,
        )


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
    """Delete generated output/cache directories. Returns human-readable notes."""
    notes: list[str] = []
    for path in dirs:
        path = Path(path)
        if not path.exists():
            notes.append(f"skipped missing: {path}")
            continue
        shutil.rmtree(path)
        notes.append(f"deleted: {path}")
    return notes


def _save_original(
    png_bytes: bytes,
    cache_dir: Path,
    scryfall_id: str,
    face_index: int | None,
) -> Path:
    path = original_cache_path(cache_dir, scryfall_id, face_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(png_bytes)
    return path


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
    )
    out_path = output_dir / out_name
    output_dir.mkdir(parents=True, exist_ok=True)
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
        native_scale=native_scale,
        device=device,
    )


def _upscalers_for_targets(
    model_id: UpscaleModel,
    dpi_targets: list[int],
    weights_dir: Path,
) -> dict[int, Upscaler]:
    """Build unique Upscaler instances keyed by native scale."""
    needed = {native_scale_for_dpi(d, model_id) for d in dpi_targets}
    return {
        scale: Upscaler(model=model_id, scale=scale, weights_dir=weights_dir)
        for scale in sorted(needed)
    }


def regenerate_face(
    item: FaceResult,
    *,
    output_dir: Path | None = None,
    cache_dir: Path = Path("imgcache"),
    weights_dir: Path = Path("weights"),
    dpi: int | None = None,
    model: UpscaleModel | str | None = None,
    on_progress: ProgressCallback | None = None,
) -> FaceResult:
    """Force re-upscale one face at a target DPI (bypasses upscale cache)."""
    dpi = dpi if dpi is not None else item.dpi
    model_id = parse_model(model) if model is not None else parse_model(item.model)
    native = native_scale_for_dpi(dpi, model_id)
    output_dir = output_dir or item.out_path.parent
    client = ScryfallClient()
    upscaler = Upscaler(model=model_id, scale=native, weights_dir=weights_dir)

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    face = CardFaceImage(
        scryfall_id=item.scryfall_id,
        card_name=item.card_name,
        face_name=item.face_name,
        set_code=item.set_code,
        collector_number=item.collector_number,
        png_url=item.png_url,
        face_index=item.face_index,
    )
    if item.original_path.is_file():
        png_bytes = item.original_path.read_bytes()
        log(
            f"Regenerating {dpi} DPI with {model_id.value} "
            f"(native x{native}) from {item.original_path.name}"
        )
    else:
        log(f"Re-downloading {item.png_url}")
        png_bytes = download_png(face.png_url, session=client._session)

    original_path = _save_original(
        png_bytes, cache_dir, face.scryfall_id, face.face_index
    )
    raw = load_or_upscale(
        png_bytes=png_bytes,
        upscaler=upscaler,
        cache_dir=cache_dir,
        scryfall_id=face.scryfall_id,
        face_index=face.face_index,
        force=True,
    )
    result = _write_dpi_variant(
        face=face,
        raw=raw.image,
        original_path=original_path,
        output_dir=output_dir,
        model_id=model_id,
        dpi=dpi,
        native_scale=native,
        device=raw.device,
    )
    log(f"  regenerated {result.out_path} ({result.out_path.stat().st_size} bytes)")
    return result


def process_entries(
    entries: list[DeckEntry],
    *,
    output_dir: Path,
    dpi: int = DEFAULT_DPI,
    all_dpis: bool = False,
    model: UpscaleModel | str = UpscaleModel.SWINIR,
    cache_dir: Path = Path("imgcache"),
    weights_dir: Path = Path("weights"),
    skip_existing: bool = False,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
    on_face_done: FaceDoneCallback | None = None,
) -> PipelineResult:
    """Resolve, download, upscale, and write PNGs for each deck entry."""
    result = PipelineResult()
    if not entries:
        return result

    model_id = parse_model(model)
    dpi_targets = resolve_dpi_targets(dpi=dpi, all_dpis=all_dpis)
    upscalers = _upscalers_for_targets(model_id, dpi_targets, Path(weights_dir))

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

    for entry in entries:
        try:
            card, warnings = client.resolve(entry)
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
                    out_name = output_filename(
                        face.face_name,
                        face.set_code,
                        face.collector_number,
                        face.face_label,
                        model_id,
                        target_dpi,
                    )
                    out_path = output_dir / out_name

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
                            native_scale=native,
                            device=read_cache_device(cached),
                        )
                        log(f"  exists: {out_name}")
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
