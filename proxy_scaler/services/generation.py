"""Generation-enqueue orchestration, extracted from proxy_scaler/ui/decklist.py
during the Streamlit -> FastAPI migration. Pure functions, no session-state
coupling — the old Streamlit version stashed newly-queued task ids into
st.session_state.pending_task_ids so a session could track its own
in-flight work; callers here (the API layer) get those ids back as a
return value instead and are responsible for tracking them client-side.
"""

from __future__ import annotations

from pathlib import Path

from proxy_scaler import db
from proxy_scaler.pipeline import FaceResult, output_filename
from proxy_scaler.scryfall import ScryfallClient, ScryfallError, expand_faces


def enqueue_face(
    *,
    scryfall_id: str,
    face_index: int | None,
    face_label: str | None,
    face_name: str,
    card_name: str,
    set_code: str,
    collector_number: str,
    png_url: str,
    dpi_targets: list[int],
    model: str,
    tile_size: int,
    output_dir: Path,
    cache_dir: Path,
    weights_dir: Path,
    project_tag: str | None,
    db_path: Path | str | None = None,
) -> list[int]:
    """Queue one task per requested DPI for an already-resolved face (no
    Scryfall call needed — caller already has scryfall_id/png_url/etc, from
    either a fresh batch resolve or an existing gallery FaceResult).
    Returns the new task ids."""
    return [
        db.enqueue_task(
            project_tag,
            scryfall_id=scryfall_id,
            face_index=face_index,
            face_label=face_label,
            face_name=face_name,
            card_name=card_name,
            set_code=set_code,
            collector_number=collector_number,
            png_url=png_url,
            dpi=dpi,
            model=model,
            tile_size=tile_size,
            output_dir=str(output_dir),
            cache_dir=str(cache_dir),
            weights_dir=str(weights_dir),
            db_path=db_path,
        )
        for dpi in dpi_targets
    ]


def active_task_keys(
    project_tag: str | None, db_path: Path | str | None = None
) -> set[tuple[str, int | None, int, str]]:
    """(scryfall_id, face_index, dpi, model) keys with a pending/running
    task for this project. Enqueueing must skip these too, not just
    disk-existing files — otherwise enqueueing again before a previous
    batch finishes would queue duplicate work for an image already in
    flight."""
    if project_tag is None:
        return set()
    tasks = db.list_tasks(
        project_tag=project_tag, statuses=["pending", "running"], db_path=db_path
    )
    return {(t.scryfall_id, t.face_index, t.dpi, t.model) for t in tasks}


def enqueue_decklist_entries(
    entries: list,
    *,
    model: str,
    dpi_targets: list[int],
    skip_existing: bool,
    tile_size: int,
    output_dir: Path,
    cache_dir: Path,
    weights_dir: Path,
    project_tag: str | None,
    on_note=None,
    db_path: Path | str | None = None,
) -> tuple[int, int, list[int]]:
    """Resolve entries (one batched Scryfall call, not one per card) and
    queue one task per (face, dpi) that's actually missing: not already
    satisfied on disk (when skip_existing) and not already pending/running
    for this project (always — regardless of skip_existing, duplicating
    in-flight work is never wanted). Returns (queued_count, failed_count,
    task_ids)."""
    client = ScryfallClient()
    resolved = client.resolve_many(entries)
    active = active_task_keys(project_tag, db_path=db_path)
    queued = 0
    failed = 0
    task_ids: list[int] = []
    seen_keys: set[str] = set()
    # Diagnostic counters only (don't affect enqueueing) — surfaced as a
    # note when queued ends up at 0, so "nothing to do" is self-explanatory
    # instead of a silent no-op indistinguishable from a real bug. See the
    # two `continue` sites below for what each one counts.
    skipped_existing = 0
    skipped_active = 0
    for entry, pre in zip(entries, resolved):
        try:
            if isinstance(pre, ScryfallError):
                raise pre
            card, warnings = pre
            for w in warnings:
                if on_note:
                    on_note(w)
            for face in expand_faces(card):
                face_key = f"{face.scryfall_id}:{face.face_index}"
                if face_key in seen_keys:
                    continue
                seen_keys.add(face_key)

                targets_needed = []
                for target_dpi in dpi_targets:
                    if (face.scryfall_id, face.face_index, target_dpi, model) in active:
                        skipped_active += 1
                        continue
                    out_name = output_filename(
                        face.face_name,
                        face.set_code,
                        face.collector_number,
                        face.face_label,
                        model,
                        target_dpi,
                    )
                    out_path = output_dir / out_name
                    if skip_existing and out_path.exists():
                        skipped_existing += 1
                        # No task will ever run for this face/dpi under
                        # the current project_tag (that's the whole point
                        # of skipping it) — without this, the file exists
                        # on disk but the gallery has no row for it under
                        # this project_tag, so the UI shows "not generated
                        # yet" for an image that's actually sitting right
                        # there. Register it now instead of only ever
                        # discovering it via a real task completion.
                        db.upsert_gallery_item(
                            project_tag,
                            FaceResult(
                                out_path=out_path,
                                original_path=Path(""),
                                scryfall_id=face.scryfall_id,
                                face_index=face.face_index,
                                face_name=face.face_name,
                                card_name=face.card_name,
                                set_code=face.set_code,
                                collector_number=face.collector_number,
                                png_url=face.png_url,
                                dpi=target_dpi,
                                model=model,
                                face_label=face.face_label,
                            ),
                            db_path=db_path,
                        )
                        continue
                    targets_needed.append(target_dpi)
                if not targets_needed:
                    continue

                new_ids = enqueue_face(
                    scryfall_id=face.scryfall_id,
                    face_index=face.face_index,
                    face_label=face.face_label,
                    face_name=face.face_name,
                    card_name=face.card_name,
                    set_code=face.set_code,
                    collector_number=face.collector_number,
                    png_url=face.png_url,
                    dpi_targets=targets_needed,
                    model=model,
                    tile_size=tile_size,
                    output_dir=output_dir,
                    cache_dir=cache_dir,
                    weights_dir=weights_dir,
                    project_tag=project_tag,
                    db_path=db_path,
                )
                task_ids.extend(new_ids)
                queued += len(targets_needed)
        except ScryfallError as exc:
            failed += 1
            if on_note:
                on_note(f"FAIL [{entry.raw_line}]: {exc}")

    if queued == 0 and on_note:
        if skipped_existing:
            on_note(
                f"{skipped_existing} image(s) already exist in {output_dir} "
                "(skip existing is on)."
            )
        if skipped_active:
            on_note(f"{skipped_active} image(s) already queued or running.")

    return queued, failed, task_ids
