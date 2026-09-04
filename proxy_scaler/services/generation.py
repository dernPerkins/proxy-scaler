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
from proxy_scaler.card_lookup import CardResolver
from proxy_scaler.dpi import CUSTOM_SOURCE_MODEL, ORIGINAL_DPI, ORIGINAL_MODEL
from proxy_scaler.pipeline import FaceResult, output_filename
from proxy_scaler.scryfall import CardFaceImage, ScryfallError, expand_faces
from proxy_scaler.upscale import original_cache_path


def custom_face(entry) -> CardFaceImage:
    """The single face of a Custom Image entry.

    Custom Images bypass resolution entirely — there is nothing to look up,
    the client already knows the content hash and the name it wants. Always
    single-faced: an uploaded image is one side of one card, and a user
    wanting a double-faced custom uploads two of them.
    """
    name = (entry.name or "").strip() or "Custom card"
    return CardFaceImage(
        scryfall_id="",
        card_name=name,
        face_name=name,
        set_code="",
        collector_number="",
        png_url="",
        face_index=None,
        total_faces=1,
        lang="en",
        custom_hash=entry.custom_hash,
    )


def _faces_for(entry, pre, on_note) -> list[CardFaceImage]:
    """Faces to generate for one decklist entry, from either world."""
    if getattr(entry, "custom_hash", None):
        return [custom_face(entry)]
    if isinstance(pre, ScryfallError):
        raise pre
    card, warnings = pre
    for w in warnings:
        if on_note:
            on_note(w)
    return list(expand_faces(card))


def _custom_source_dpi(face: CardFaceImage) -> int:
    """True print DPI of an uploaded image at card size, read from the
    bytes this server holds.

    Resolved at enqueue time rather than when the task runs so the value is
    fixed in the task row: the dedup key (active_task_keys), the
    skip-existing probe (find_generated_image) and the row the worker
    finally writes all have to agree on which variant this is, and
    recomputing it in two places is how they would drift apart.

    Raised as ScryfallError purely to land in the caller's existing
    per-entry failure handler — it is not a Scryfall problem, but "this one
    entry could not be prepared, note it and carry on with the rest" is
    exactly the behaviour wanted, and inventing a parallel exception would
    duplicate that handling for no gain.
    """
    from proxy_scaler import customs

    dpi = customs.source_dpi(face.custom_hash)
    if dpi is None:
        raise ScryfallError(
            f"'{face.card_name}' has not been uploaded to this server yet — "
            "the client syncs custom images on demand, so retry the action "
            "that needed it."
        )
    return round(dpi)


def _resolve_pairs(entries: list, resolver: CardResolver) -> list:
    """Pair each entry with its resolution result, resolving only the ones
    that need it. Custom Images get None — they are not Scryfall cards and
    must never reach the resolver, which would at best waste a lookup and
    at worst match some unrelated real card by name."""
    needs = [e for e in entries if not getattr(e, "custom_hash", None)]
    resolved = iter(resolver.resolve_many(needs)) if needs else iter(())
    return [
        None if getattr(e, "custom_hash", None) else next(resolved) for e in entries
    ]


def enqueue_face(
    *,
    scryfall_id: str | None,
    face_index: int | None,
    face_label: str | None,
    face_name: str,
    card_name: str,
    set_code: str,
    collector_number: str,
    png_url: str,
    dpi_targets: list[int],
    custom_hash: str | None = None,
    model: str,
    tile_size: int,
    output_dir: Path,
    cache_dir: Path,
    weights_dir: Path,
    project_tag: str | None,
    total_faces: int | None = None,
    lang: str = "en",
    force: bool = False,
    db_path: Path | str | None = None,
) -> list[int]:
    """Queue one task per requested DPI for an already-resolved face (no
    Scryfall call needed — caller already has scryfall_id/png_url/etc, from
    either a fresh batch resolve or an existing gallery FaceResult).
    Returns the new task ids.

    force=False (first generation, the default) lets the worker reuse the
    x4 upscale cache — sibling DPI tasks of one face then share a single
    model pass. force=True is the user-initiated Regenerate path: bypass
    the cache and re-run inference."""
    return [
        db.enqueue_task(
            project_tag,
            # The database CHECK requires exactly one identity, so a custom
            # face must send scryfall_id as NULL rather than "".
            scryfall_id=(scryfall_id or None) if not custom_hash else None,
            custom_hash=custom_hash,
            face_index=face_index,
            face_label=face_label,
            face_name=face_name,
            card_name=card_name,
            set_code=set_code or None,
            collector_number=collector_number or None,
            png_url=png_url or None,
            dpi=dpi,
            model=model,
            tile_size=tile_size,
            output_dir=str(output_dir),
            cache_dir=str(cache_dir),
            weights_dir=str(weights_dir),
            total_faces=total_faces,
            lang=lang,
            force=force,
            db_path=db_path,
        )
        for dpi in dpi_targets
    ]


def active_task_keys(
    project_tag: str | None, db_path: Path | str | None = None
) -> set[tuple[str, int | None, int, str]]:
    """(identity, face_index, dpi, model) keys with a pending/running task
    for this project, where identity is a Scryfall id or 'custom:<sha256>'
    (see customs.identity_key). Enqueueing must skip these too, not just
    disk-existing files — otherwise enqueueing again before a previous
    batch finishes would queue duplicate work for an image already in
    flight."""
    if project_tag is None:
        return set()
    tasks = db.list_tasks(
        project_tag=project_tag, statuses=["pending", "running"], db_path=db_path
    )
    return {(t.identity_key, t.face_index, t.dpi, t.model) for t in tasks}


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
    card_db_path: Path | str | None = None,
) -> tuple[int, int, list[int]]:
    """Resolve entries (one batched Scryfall call, not one per card) and
    queue one task per (face, dpi) that's actually missing: not already
    satisfied on disk (when skip_existing) and not already pending/running
    for this project (always — regardless of skip_existing, duplicating
    in-flight work is never wanted). Returns (queued_count, failed_count,
    task_ids)."""
    # Local-first: answered from the imported card corpus when possible,
    # live Scryfall only for the leftovers (see card_lookup.CardResolver).
    resolver = CardResolver(card_db_path=card_db_path)
    resolved = _resolve_pairs(entries, resolver)
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
            for face in _faces_for(entry, pre, on_note):
                face_key = f"{face.identity_key}:{face.face_index}"
                if face_key in seen_keys:
                    continue
                seen_keys.add(face_key)

                targets_needed = []
                for target_dpi in dpi_targets:
                    if (face.identity_key, face.face_index, target_dpi, model) in active:
                        skipped_active += 1
                        continue
                    if skip_existing:
                        # Registry-first: the generated_images registry is
                        # the authority on what exists, so a known variant
                        # needs only a liveness stat of its recorded path
                        # — no filename reconstruction, and this project
                        # just joins the image via a membership.
                        known = db.find_generated_image(
                            face.identity_key,
                            face.face_index,
                            model,
                            target_dpi,
                            db_path=db_path,
                        )
                        if known is not None and Path(known["out_path"]).exists():
                            skipped_existing += 1
                            db.add_membership(
                                project_tag, known["id"], db_path=db_path
                            )
                            continue
                    out_path = output_dir / output_filename(
                        face.face_name,
                        face.set_code,
                        face.collector_number,
                        face.face_label,
                        model,
                        target_dpi,
                        lang=face.lang,
                        scryfall_id=face.scryfall_id,
                        custom_hash=face.custom_hash,
                    )
                    # Custom Images have only ever had one filename shape,
                    # so there is no legacy name to fall back to.
                    if not out_path.exists() and not face.is_custom:
                        # Legacy-named files (predating the embedded-id
                        # filename format) are never renamed on disk — an
                        # existing one still satisfies this face/dpi.
                        legacy_path = output_dir / output_filename(
                            face.face_name,
                            face.set_code,
                            face.collector_number,
                            face.face_label,
                            model,
                            target_dpi,
                            lang=face.lang,
                        )
                        if legacy_path.exists():
                            out_path = legacy_path
                    if skip_existing and out_path.exists():
                        skipped_existing += 1
                        # The pre-upscale cached original lives at a
                        # deterministic path keyed only by scryfall_id/
                        # face_index (see upscale.py::original_cache_path)
                        # — independent of dpi/model, so it's very likely
                        # still sitting in cache_dir from whatever earlier
                        # run produced this output file, even though no
                        # task ever ran for it under the *current*
                        # project_tag. Stored unconditionally, whether or
                        # not it currently exists: gallery.py's file-serving
                        # route already 404s gracefully for a stored path
                        # that isn't actually there (same as it would for
                        # any other missing file), so there's no need for a
                        # separate "no original" sentinel here — and if a
                        # later regen ever writes a file to this exact
                        # deterministic path, "Compare" starts working
                        # without needing another gallery upsert at all.
                        original_path = original_cache_path(
                            cache_dir,
                            face.scryfall_id,
                            face.face_index,
                            custom_hash=face.custom_hash,
                        )
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
                                original_path=original_path,
                                scryfall_id=face.scryfall_id or None,
                                custom_hash=face.custom_hash,
                                face_index=face.face_index,
                                face_name=face.face_name,
                                card_name=face.card_name,
                                set_code=face.set_code,
                                collector_number=face.collector_number,
                                png_url=face.png_url,
                                dpi=target_dpi,
                                model=model,
                                face_label=face.face_label,
                                total_faces=face.total_faces,
                                lang=face.lang,
                            ),
                            db_path=db_path,
                        )
                        continue
                    targets_needed.append(target_dpi)
                if not targets_needed:
                    continue

                new_ids = enqueue_face(
                    scryfall_id=face.scryfall_id,
                    custom_hash=face.custom_hash,
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
                    total_faces=face.total_faces,
                    lang=face.lang,
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


def enqueue_download_entries(
    entries: list,
    *,
    output_dir: Path,
    cache_dir: Path,
    weights_dir: Path,
    project_tag: str | None,
    on_note=None,
    db_path: Path | str | None = None,
    card_db_path: Path | str | None = None,
) -> tuple[int, int, list[int]]:
    """Resolve entries and queue one download-only task per face whose
    Scryfall original isn't already cached — the download-only analogue of
    enqueue_decklist_entries, using the (ORIGINAL_DPI, ORIGINAL_MODEL)
    sentinel variant. Skip-existing is always on here: a cached original
    is shared by every variant of a face, so re-downloading an existing
    one is never useful in a batch (Re-Fetch, which deliberately
    overwrites, goes through gallery.py's refetch route instead).
    output_dir/weights_dir are threaded through only because
    db.enqueue_task requires them NOT NULL — the download handler ignores
    both. Returns (queued_count, failed_count, task_ids)."""
    resolver = CardResolver(card_db_path=card_db_path)
    resolved = _resolve_pairs(entries, resolver)
    active = active_task_keys(project_tag, db_path=db_path)
    queued = 0
    failed = 0
    task_ids: list[int] = []
    seen_keys: set[str] = set()
    skipped_existing = 0
    skipped_active = 0
    for entry, pre in zip(entries, resolved):
        try:
            for face in _faces_for(entry, pre, on_note):
                face_key = f"{face.identity_key}:{face.face_index}"
                if face_key in seen_keys:
                    continue
                seen_keys.add(face_key)

                # A Custom Image has no Scryfall original to download; its
                # source-variant registration is the CUSTOM_SOURCE_MODEL
                # sentinel instead, which is what makes an un-upscaled
                # upload printable. See proxy_scaler/dpi.py.
                variant_model = (
                    CUSTOM_SOURCE_MODEL if face.is_custom else ORIGINAL_MODEL
                )
                variant_dpi = _custom_source_dpi(face) if face.is_custom else ORIGINAL_DPI

                if (
                    face.identity_key,
                    face.face_index,
                    variant_dpi,
                    variant_model,
                ) in active:
                    skipped_active += 1
                    continue

                original_path = original_cache_path(
                    cache_dir,
                    face.scryfall_id,
                    face.face_index,
                    custom_hash=face.custom_hash,
                )
                known = db.find_generated_image(
                    face.identity_key,
                    face.face_index,
                    variant_model,
                    variant_dpi,
                    db_path=db_path,
                )
                if known is not None and Path(known["out_path"]).exists():
                    skipped_existing += 1
                    db.add_membership(project_tag, known["id"], db_path=db_path)
                    continue
                if original_path.exists():
                    # The original is already on disk (downloaded as part of
                    # an earlier upscale run, or under another project) but
                    # has no download-variant registry row — register it so
                    # the UI shows the badge, same rationale as the
                    # skip-existing backfill in enqueue_decklist_entries.
                    skipped_existing += 1
                    db.upsert_gallery_item(
                        project_tag,
                        FaceResult(
                            out_path=original_path,
                            original_path=original_path,
                            scryfall_id=face.scryfall_id or None,
                            custom_hash=face.custom_hash,
                            face_index=face.face_index,
                            face_name=face.face_name,
                            card_name=face.card_name,
                            set_code=face.set_code,
                            collector_number=face.collector_number,
                            png_url=face.png_url,
                            dpi=variant_dpi,
                            model=variant_model,
                            face_label=face.face_label,
                            native_scale=1,
                            total_faces=face.total_faces,
                            lang=face.lang,
                        ),
                        db_path=db_path,
                    )
                    continue

                new_ids = enqueue_face(
                    scryfall_id=face.scryfall_id,
                    custom_hash=face.custom_hash,
                    face_index=face.face_index,
                    face_label=face.face_label,
                    face_name=face.face_name,
                    card_name=face.card_name,
                    set_code=face.set_code,
                    collector_number=face.collector_number,
                    png_url=face.png_url,
                    dpi_targets=[variant_dpi],
                    model=variant_model,
                    tile_size=0,
                    output_dir=output_dir,
                    cache_dir=cache_dir,
                    weights_dir=weights_dir,
                    project_tag=project_tag,
                    total_faces=face.total_faces,
                    lang=face.lang,
                    db_path=db_path,
                )
                task_ids.extend(new_ids)
                queued += 1
        except ScryfallError as exc:
            failed += 1
            if on_note:
                on_note(f"FAIL [{entry.raw_line}]: {exc}")

    if queued == 0 and on_note:
        if skipped_existing:
            on_note(
                f"{skipped_existing} original(s) already downloaded to "
                f"{cache_dir / 'originals'}."
            )
        if skipped_active:
            on_note(f"{skipped_active} download(s) already queued or running.")

    return queued, failed, task_ids
