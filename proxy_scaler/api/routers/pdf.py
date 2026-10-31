from __future__ import annotations

import base64
import io
import re
import threading
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from PIL import Image

from proxy_scaler import db
from proxy_scaler.api.deps import get_db_path
from proxy_scaler.api.entries import to_deck_entry
from proxy_scaler.api.schemas import (
    CornerRadiusIn,
    BackImageIn,
    CutterIn,
    GuideCornersIn,
    GuideLineIn,
    RectOut,
    PdfJobOut,
    PdfJobStatusOut,
    PdfLayoutIn,
    PdfPagePreviewOut,
    PdfPageSlotOut,
    PdfPreviewOut,
)
from proxy_scaler.decklist import DeckEntry
from proxy_scaler import pdf_jobs
from proxy_scaler.pdf_jobs import PdfRenderCanceled
from proxy_scaler import backs
from proxy_scaler.cut_file import build_cut_file_svg
from proxy_scaler.pdf_layout import (
    ALPHA_SET_CODE,
    CornerRadius,
    CARD_HEIGHT_MM,
    CARD_WIDTH_MM,
    MM_PER_IN,
    BackSource,
    CutterMarkStyle,
    CutterOrientation,
    FlipEdge,
    GuideCorners,
    GuideLine,
    GuideVisibility,
    PageLayout,
    PageOrder,
    PrintSlot,
    Rect,
    RegistrationMarks,
    ReverseFill,
    _describe_face,
    back_pages_are_rotated,
    add_bleed,
    back_page_cells,
    fit_bled_image,
    build_pdf,
    hex_to_rgb,
    build_print_slots,
    match_quantities,
    paginate,
    cell_corner_radii,
    printing_from_originals,
    registration_conflict,
    registration_keep_out,
    registration_mark_rects,
    resolve_page_layout,
    resolve_reverse,
    rgb_to_hex,
    unique_image_count,
)
from proxy_scaler import customs
from proxy_scaler.pipeline import FaceResult, ensure_original_thumbnail

router = APIRouter(prefix="/api/pdf", tags=["pdf"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    return slug or "proxy-scaler"


def _default_pdf_basename(today: date | None = None) -> str:
    """Filename stem for a PDF whose project has no name (an Unnamed
    Project). Never the project_tag: that's an opaque 32-char hex string,
    which is no name to hand a user saving a file. The desktop client
    builds the same string for its save-dialog default
    (pdfFilename.ts::defaultPdfBasename) so the offered name and the one
    in Content-Disposition agree — bar a remote generation host sitting on
    the other side of local midnight from the client, where the two dates
    can differ by a day."""
    return f"proxy-scaler-{(today or date.today()).isoformat()}"


# Shared with export.py, which imports it from here.
_to_deck_entry = to_deck_entry


def back_manifest(
    back_images: list[BackImageIn],
    *,
    back_image_hash: str | None,
    back_image_includes_bleed: bool,
    back_image_bleed_mm: float | None,
) -> list[BackImageIn]:
    """The request's Back Image manifest with the universal back folded
    in. A client that predates per-card backs sends only the legacy trio,
    and a current one may still leave its Selected Back out of the list;
    either way the inherit target has to be resolvable, so it is
    synthesized from those fields when the list lacks it. The manifest
    wins where both name the same hash — it is the newer declaration."""
    manifest = list(back_images)
    if back_image_hash and all(b.hash != back_image_hash for b in manifest):
        manifest.append(
            BackImageIn(
                hash=back_image_hash,
                includes_bleed=back_image_includes_bleed,
                bleed_mm=back_image_bleed_mm,
            )
        )
    return manifest


def resolve_back_manifest(
    manifest: list[BackImageIn],
    *,
    preferred_dpi: int | None,
    preferred_model: str | None,
    db_path,
) -> dict[str, BackSource]:
    """Every manifest hash this server holds, as the file to print plus
    the client's bleed declaration for it. A hash not synced here is
    simply absent — whether that is an error depends on who needs it,
    which is the caller's call (see _prepare / export._prepare_export).
    A malformed hash is a 400: it reaches the filesystem."""
    resolved: dict[str, BackSource] = {}
    for back in manifest:
        try:
            # The same preferences the cards are matched with: an
            # upscaled back is picked like an upscaled custom.
            path = backs.resolve_print_source(
                back.hash,
                preferred_dpi=preferred_dpi,
                preferred_model=preferred_model,
                db_path=db_path,
            )
        except backs.BackImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if path is not None:
            resolved[back.hash] = BackSource(
                path, includes_bleed=back.includes_bleed, bleed_mm=back.bleed_mm
            )
    return resolved


@dataclass(frozen=True)
class PreparedRender:
    """Everything both the preview and the render need, resolved once.

    Bundled rather than returned as a widening tuple: back printing added
    four more things to carry, and a seven-element tuple is a bug waiting
    for someone to unpack it in the wrong order.
    """

    layout: PageLayout
    back_layout: PageLayout
    pages: list[list[PrintSlot]]
    missing: list[str]
    missing_at_dpi: list[str]
    from_originals: list[str]
    # Every Back Image the request named that this server holds, by
    # content hash, and the hash a card with no choice of its own
    # inherits (the Selected Back). See pdf_layout.resolve_reverse.
    back_images: dict[str, BackSource]
    default_back_hash: str | None
    reverses_needing_back_image: int
    # Cards that chose their own Back Image but whose hash this server
    # doesn't hold (not in the manifest, or never synced). Human
    # descriptions, for the 400.
    missing_per_card_backs: list[str]

    @property
    def back_image_path(self) -> Path | None:
        """The Selected Back's file, if the request named one and this
        server holds it."""
        back = self.back_images.get(self.default_back_hash) if self.default_back_hash else None
        return back.path if back is not None else None

    @property
    def missing_back_image(self) -> bool:
        """Back printing is on, at least one Reverse would take the Back
        Image, and there is no usable one. Only an error when a Reverse
        would actually come up empty — an all-double-faced sheet
        legitimately needs no Back Image."""
        return self.reverses_needing_back_image > 0 and self.back_image_path is None

    @property
    def total_page_count(self) -> int:
        return len(self.pages)


def _guides(body: PdfLayoutIn) -> GuideVisibility:
    return GuideVisibility(
        hide_card_guides_front=body.hide_card_guides_front,
        hide_page_guides_front=body.hide_page_guides_front,
        hide_card_guides_back=body.hide_card_guides_back,
        hide_page_guides_back=body.hide_page_guides_back,
    )


def _registration(body: PdfLayoutIn) -> RegistrationMarks | None:
    """None when no cutter is selected — the sheet carries no marks and
    the guides are not clipped, exactly the pre-cutter output."""
    if body.cutter is CutterIn.NONE:
        return None
    return RegistrationMarks(
        style=CutterMarkStyle(body.cutter_mark_style.value),
        orientation=CutterOrientation(body.cutter_orientation.value),
        inset_mm=body.cutter_inset_mm,
        hide_front=body.hide_cutter_marks_front,
        hide_back=body.hide_cutter_marks_back,
    )


def _layout_from_body(body: PdfLayoutIn, offset_x: float, offset_y: float) -> PageLayout:
    """Pure geometry from the request — no database, no entries — so the
    cut-file route can share it with _prepare."""
    return resolve_page_layout(
        page_w_mm=body.page_width_mm,
        page_h_mm=body.page_height_mm,
        cols=body.cols,
        rows=body.rows,
        bleed_mm=body.bleed_mm,
        spacing_x_mm=body.spacing_x_mm,
        spacing_y_mm=body.spacing_y_mm,
        offset_x_mm=offset_x,
        offset_y_mm=offset_y,
        guide_width_pt=body.guide_width_pt,
        guide_length_mm=body.guide_length_mm,
        page_guide_width_pt=body.page_guide_width_pt,
        card_guide_color=hex_to_rgb(body.card_guide_color),
        page_guide_color=hex_to_rgb(body.page_guide_color),
        card_guide_line=GuideLine(body.card_guide_line.value),
        page_guide_line=GuideLine(body.page_guide_line.value),
        card_guide_corners=GuideCorners(body.card_guide_corners.value),
        corner_radius=CornerRadius(body.corner_radius.value),
        custom_corner_radius_mm=body.custom_corner_radius_mm,
    )


def _rect_out(r: Rect) -> RectOut:
    return RectOut(x_mm=r.x, y_mm=r.y, w_mm=r.w, h_mm=r.h)



def gallery_for_print(project_tag: str, entries: list[DeckEntry], db_path) -> list[FaceResult]:
    """The project's gallery plus every registry row of the Custom Images
    it prints (as cards or on a card's face), whatever tag made them.
    Custom Images belong to the
    machine, so a variant upscaled from the Customs tab (dpi.LIBRARY_TAG)
    or by another project is as printable here as one this project made;
    match_quantities and _pick_dpi_variant then apply the preferred
    DPI/model to customs exactly as they do to cards. A face override
    printing another printing's face pulls that face's rows in the same
    way, so it prints even before the project has adopted them. Deduped by
    registry id, and the declared bleed is attached to every custom row."""
    raw_items = db.list_gallery_items(project_tag, db_path=db_path)
    seen = {int(d["id"]) for d in raw_items}

    def _add(rows: list[dict]) -> None:
        for d in rows:
            if int(d["id"]) not in seen:
                seen.add(int(d["id"]))
                raw_items.append(d)

    custom_hashes = {e.custom_hash for e in entries if e.custom_hash}
    # Customs printed on a face of a Scryfall card (face overrides) too.
    custom_hashes |= {
        o.custom_hash for e in entries for o in e.face_overrides if o.custom_hash
    }
    for custom_hash in custom_hashes:
        _add(db.list_registry_items_for_custom(custom_hash, db_path=db_path))
    printing_faces: dict[str, set[int]] = {}
    for e in entries:
        for o in e.face_overrides:
            if o.scryfall_id:
                printing_faces.setdefault(o.scryfall_id, set()).add(o.source_position)
    for scryfall_id, faces in printing_faces.items():
        # Only the overriding face: the printing's other face is not part
        # of the deck.
        _add(
            [
                d
                for d in db.list_registry_items_for_printing(scryfall_id, db_path=db_path)
                if (d["face_index"] or 0) in faces
            ]
        )
    items = [FaceResult.from_dict(d) for d in raw_items]
    customs.attach_bleed(items)
    return items

def _prepare(body: PdfLayoutIn) -> PreparedRender:
    if not body.entries:
        raise HTTPException(status_code=400, detail="No cards to print.")
    db_path = get_db_path()
    entries = [_to_deck_entry(e) for e in body.entries]
    items = gallery_for_print(body.project_tag, entries, db_path)
    units, missing, missing_at_dpi = match_quantities(
        entries,
        items,
        # use_originals narrows Scryfall faces only; the preferred pair
        # still picks among a custom's variants.
        preferred_dpi=body.preferred_dpi,
        preferred_model=body.preferred_model,
        use_originals=body.use_originals,
    )

    layout = _layout_from_body(body, body.offset_x_mm, body.offset_y_mm)
    # Back Pages carry their own offset ON TOP of nothing — not on top of
    # the front's. The two are independent calibrations of two physical
    # passes through the printer, and adding them would make nudging the
    # fronts silently move the backs too.
    back_layout = _layout_from_body(body, body.back_offset_x_mm, body.back_offset_y_mm)

    # Pairing only means anything while back printing is on: with it off,
    # a Back Face has no Reverse to live on and stays its own card, which
    # is exactly the historical behaviour.
    slots = build_print_slots(
        units, pair_back_faces=body.back_printing and body.back_faces_as_reverse
    )
    pages = paginate(slots, layout.cards_per_page)

    back_images: dict[str, BackSource] = {}
    default_back_hash = body.back_image_hash or None
    reverses_needing_back_image = 0
    missing_per_card_backs: list[str] = []
    if body.back_printing:
        back_images = resolve_back_manifest(
            back_manifest(
                body.back_images,
                back_image_hash=body.back_image_hash,
                back_image_includes_bleed=body.back_image_includes_bleed,
                back_image_bleed_mm=body.back_image_bleed_mm,
            ),
            preferred_dpi=body.preferred_dpi,
            preferred_model=body.preferred_model,
            db_path=db_path,
        )
        reverse_fill = ReverseFill(body.reverse_fill.value)
        for page in pages:
            for slot in page:
                if slot.reverse is not None or slot.back_mode == "blank":
                    continue
                if slot.back_mode == "image":
                    if slot.back_hash not in back_images:
                        missing_per_card_backs.append(_describe_face(slot.front))
                    continue
                # Counted only when a Back Image is what would actually go
                # there. With reverse_fill=BLANK those same Reverses are
                # meant to be empty, so they need nothing and must not be
                # reported as missing anything.
                if reverse_fill is ReverseFill.BACK_IMAGE:
                    reverses_needing_back_image += 1

    return PreparedRender(
        layout=layout,
        back_layout=back_layout,
        pages=pages,
        missing=missing,
        missing_at_dpi=missing_at_dpi,
        from_originals=printing_from_originals(
            (u.best for u in units), use_originals=body.use_originals
        ),
        back_images=back_images,
        default_back_hash=default_back_hash,
        reverses_needing_back_image=reverses_needing_back_image,
        missing_per_card_backs=missing_per_card_backs,
    )


def _render_kwargs(body: PdfLayoutIn, prepared: PreparedRender) -> dict:
    """The back-printing half of build_pdf's arguments, shared by the
    synchronous route and the job thread so the two can't drift."""
    return dict(
        guides=_guides(body),
        back_printing=body.back_printing,
        back_layout=prepared.back_layout,
        back_images=prepared.back_images,
        default_back_hash=prepared.default_back_hash,
        flip_edge=FlipEdge(body.flip_edge.value),
        page_order=PageOrder(body.page_order.value),
        reverse_fill=ReverseFill(body.reverse_fill.value),
        registration=_registration(body),
    )


def _guard_printable(prepared: PreparedRender) -> None:
    """Refuse a render that would produce blank Reverses. Stated as an
    error rather than silently printing empty backs: the user finds out
    either way, but this way it costs no cardstock."""
    if not prepared.pages:
        raise HTTPException(status_code=400, detail="Nothing to print — no matched cards.")
    if prepared.missing_per_card_backs:
        # Distinct names, in sheet order: one card printed four times is
        # one thing for the user to fix, not four.
        names = list(dict.fromkeys(prepared.missing_per_card_backs))
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(prepared.missing_per_card_backs)} card(s) use a back image that "
                f"isn't synced to this server: {', '.join(names)}"
            ),
        )
    if prepared.missing_back_image:
        raise HTTPException(
            status_code=400,
            detail=(
                "Back printing is on, but no back image is available for "
                f"{prepared.reverses_needing_back_image} card(s). Pick one on the "
                "Backs tab, or turn back printing off."
            ),
        )


@router.post("/preview", response_model=PdfPreviewOut)
def preview(body: PdfLayoutIn) -> PdfPreviewOut:
    prepared = _prepare(body)
    total_units = sum(len(p) for p in prepared.pages)
    # page_count stays the number of Front Pages — the count of sheets you
    # feed the printer — while total_page_count is what the PDF actually
    # contains. Back printing doubles the second and leaves the first
    # alone, and conflating them would double the "slots left on your last
    # page" arithmetic the client does from it.
    return PdfPreviewOut(
        units=total_units,
        missing=prepared.missing,
        page_count=len(prepared.pages),
        missing_at_dpi=prepared.missing_at_dpi,
        from_originals=prepared.from_originals,
        reverses_needing_back_image=prepared.reverses_needing_back_image,
        missing_back_image=prepared.missing_back_image,
        total_page_count=len(prepared.pages) * (2 if body.back_printing else 1),
    )


def _preview_cell(
    cell: PrintSlot | None, *, is_back: bool, prepared: PreparedRender, reverse_fill: ReverseFill
) -> tuple[FaceResult | None, BackSource | None]:
    """What one grid position shows in the page preview: (face, back_image).
    (None, None) is a genuinely empty position — no card there, or a
    Reverse that prints blank. Decided by resolve_reverse so the preview
    can never disagree with the sheet about which back a card gets."""
    if cell is None:
        return None, None
    if not is_back:
        return cell.front, None
    _path, face, back = resolve_reverse(
        cell, prepared.back_images, prepared.default_back_hash, reverse_fill
    )
    return face, back


def _preview_bled(
    thumb: Image.Image, *, bleed_mm: float, image_bleed_mm: float | None
) -> Image.Image:
    """The bleed step of a preview thumbnail, mirroring the renderer: a
    plain card thumbnail is edge-extended, a thumbnail of a file declared
    to carry `image_bleed_mm` of bleed is trimmed or topped up to the
    sheet's bleed (fit_bled_image), so the preview never shows a doubled
    border the print would not have. The synthetic preview DPI is the
    thumbnail's long edge over the long edge of whatever the file spans."""
    carried = image_bleed_mm or 0.0
    preview_dpi = max(thumb.width, thumb.height) / (
        (max(CARD_WIDTH_MM, CARD_HEIGHT_MM) + 2 * carried) / MM_PER_IN
    )
    if carried > 0:
        return fit_bled_image(
            thumb, image_bleed_mm=carried, export_dpi=preview_dpi, bleed_mm=bleed_mm
        )
    return add_bleed(thumb, dpi=preview_dpi, bleed_mm=bleed_mm)


def _back_image_thumbnail(
    path: Path | None, *, bleed_mm: float, image_bleed_mm: float | None
) -> str | None:
    """A small preview of a Back Image, built on the fly.

    Unlike a card, a Back Image has no cached original thumbnail to reuse
    (nothing generated it), so this decodes and downsamples the stored
    original each call. Bounded work: preview_page memoizes it per file,
    so each distinct back costs one decode per request no matter how many
    Reverses it fills.
    """
    if path is None or not path.is_file():
        return None
    try:
        with Image.open(path) as raw:
            thumb = raw.convert("RGB")
            thumb.thumbnail((220, 220), Image.Resampling.LANCZOS)
            bled = _preview_bled(thumb, bleed_mm=bleed_mm, image_bleed_mm=image_bleed_mm)
        buf = io.BytesIO()
        bled.save(buf, format="JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 — a preview thumbnail is never worth a 500
        return None


@router.post("/preview/page", response_model=PdfPagePreviewOut)
def preview_page(body: PdfLayoutIn) -> PdfPagePreviewOut:
    """Page-1-only visual layout preview: small (<=~50KB) thumbnails of
    each slot's *original* card art, base64-embedded directly in this one
    response — page-1-only bounds the payload to at most cols*rows
    thumbnails, which is trivially small, and avoids needing a dedicated
    file-serving route (FaceResult carries no gallery_item_id to route
    through gallery.py's existing /full,/original endpoints)."""
    prepared = _prepare(body)
    # There is no Back Page to preview when back printing is off, so the
    # flag is ignored rather than honoured — honouring it renders a page
    # that does not exist in the output, which comes out blank and looks
    # like a broken preview rather than like a setting being off.
    show_back = body.preview_back_page and body.back_printing
    layout = prepared.back_layout if show_back else prepared.layout
    pages = prepared.pages
    # The Back Page preview is the cheapest way to catch a wrong Flip Edge
    # before a sheet of cardstock pays for it, so it renders the real
    # mirrored grid rather than a mirrored-looking approximation: same
    # back_page_cells() the renderer uses, same full-grid mirroring on a
    # partial page.
    if show_back and pages:
        # Full grid, empty positions included: a Back Page's cells are
        # placed by mirrored index, so dropping the empty ones would shift
        # every later card and the preview would stop matching the sheet.
        cells = back_page_cells(
            pages[0], layout=prepared.layout, flip_edge=FlipEdge(body.flip_edge.value)
        )
    elif pages:
        # Fronts fill left-to-right from position 0, so the occupied
        # prefix IS the layout — no padding, keeping this response the
        # same shape it has always had for front previews.
        cells = list(pages[0])
    else:
        cells = []

    slots: list[PdfPageSlotOut] = []
    back_thumbnails: dict[Path, str | None] = {}
    for cell in cells:
        # Three states per grid position, not two: a card, a Back Image
        # filling a Reverse that has no Back Face, or a position no card
        # occupies at all (a partial last page, or a Reverse that prints
        # blank — which previews as exactly what it will print as). The
        # empty one has to be rendered as an empty cell rather than
        # skipped, or every slot after it shifts and the mirrored preview
        # stops matching the sheet it is previewing.
        face, back_image = _preview_cell(
            cell,
            is_back=show_back,
            prepared=prepared,
            reverse_fill=ReverseFill(body.reverse_fill.value),
        )
        if face is None and back_image is None:
            slots.append(PdfPageSlotOut(card_name="", face_label=None, model="", dpi=0))
            continue
        if back_image is not None:
            if back_image.path not in back_thumbnails:
                back_thumbnails[back_image.path] = _back_image_thumbnail(
                    back_image.path,
                    bleed_mm=body.bleed_mm,
                    # Only a declared-and-quantified back is trimmed; a
                    # bare flag (old clients) previews edge-extended like
                    # the render's fit-to-box does, near enough.
                    image_bleed_mm=(
                        back_image.bleed_mm if back_image.includes_bleed else None
                    ),
                )
            slots.append(
                PdfPageSlotOut(
                    card_name="Card back",
                    face_label=None,
                    model="",
                    dpi=0,
                    thumbnail_data_url=back_thumbnails[back_image.path],
                    is_back_image=True,
                )
            )
            continue
        thumb_path = ensure_original_thumbnail(face.original_path)
        data_url = None
        if thumb_path is not None:
            # add_bleed here (not baked into the cached thumbnail, which
            # is bleed-agnostic on purpose) is what makes the preview's
            # bleed slider actually extend the art outward instead of
            # just growing the CSS box it's stretched into — matching
            # build_pdf()'s real per-page bleed step. Cheap: this runs on
            # an already-small (~220px) thumbnail, at most cols*rows of
            # them per request.
            with Image.open(thumb_path) as thumb:
                bled = _preview_bled(
                    thumb.convert("RGB"),
                    bleed_mm=body.bleed_mm,
                    image_bleed_mm=face.custom_bleed_mm or None,
                )
            buf = io.BytesIO()
            bled.save(buf, format="JPEG", quality=85)
            data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        slots.append(
            PdfPageSlotOut(
                card_name=face.card_name,
                face_label=face.face_label,
                model=face.model,
                dpi=face.dpi,
                thumbnail_data_url=data_url,
            )
        )
    # Marks and keep-out zones for the side being previewed, in that side's
    # own frame: a Back Page uses the back layout (its own offsets) and only
    # carries marks if they are not hidden there. Deliberately not "always
    # the front layout" — the conflict warning has to describe the page
    # the user is looking at.
    # Each position's rounded Card Guide radius, from the same cells the
    # renderer draws (a Back Page's mirrored cells carry their fronts).
    for slot_out, radius in zip(slots, cell_corner_radii(cells, layout)):
        slot_out.corner_radius_mm = radius

    registration = _registration(body)
    marks: list[Rect] = []
    keep_out: list[Rect] = []
    conflict = False
    if registration is not None and registration.drawn_on(is_back=show_back):
        marks = registration_mark_rects(layout.page_w_mm, layout.page_h_mm, registration)
        keep_out = registration_keep_out(layout.page_w_mm, layout.page_h_mm, registration)
        conflict = registration_conflict(layout, keep_out)
    return PdfPagePreviewOut(
        registration_marks=[_rect_out(r) for r in marks],
        registration_keep_out=[_rect_out(r) for r in keep_out],
        registration_conflict=conflict,
        page_w_mm=layout.page_w_mm,
        page_h_mm=layout.page_h_mm,
        cols=layout.cols,
        rows=layout.rows,
        margin_x_mm=layout.margin_x_mm,
        margin_y_mm=layout.margin_y_mm,
        cell_w_mm=layout.cell_w_mm,
        cell_h_mm=layout.cell_h_mm,
        bled_card_w_mm=layout.bled_card_w_mm,
        bled_card_h_mm=layout.bled_card_h_mm,
        bleed_mm=layout.bleed_mm,
        guide_width_pt=layout.guide_width_pt,
        guide_length_mm=layout.guide_length_mm,
        page_guide_width_pt=layout.page_guide_width_pt,
        card_guide_color=rgb_to_hex(layout.card_guide_color),
        page_guide_color=rgb_to_hex(layout.page_guide_color),
        card_guide_line=GuideLineIn(layout.card_guide_line.value),
        page_guide_line=GuideLineIn(layout.page_guide_line.value),
        card_guide_corners=GuideCornersIn(layout.card_guide_corners.value),
        corner_radius=CornerRadiusIn(layout.corner_radius.value),
        custom_corner_radius_mm=layout.custom_corner_radius_mm,
        corner_radius_mm=cell_corner_radii((), layout)[0],
        # Identical for the front and back layouts — they differ only in
        # their position offsets — so one value serves whichever side is
        # being previewed.
        grid_w_mm=layout.grid_w_mm,
        grid_h_mm=layout.grid_h_mm,
        rotated=show_back
        and back_pages_are_rotated(prepared.layout, FlipEdge(body.flip_edge.value)),
        hide_card_guides=(
            body.hide_card_guides_back if show_back else body.hide_card_guides_front
        ),
        hide_page_guides=(
            body.hide_page_guides_back if show_back else body.hide_page_guides_front
        ),
        page_count=len(pages),
        slots=slots,
    )


@router.post("")
def generate_pdf(body: PdfLayoutIn) -> Response:
    """Returns the PDF as a real file response — the concrete fix for
    st.download_button silently doing nothing inside Tauri's webview
    (a known WKWebView gap around the HTML download attribute). A client
    fetch() -> blob() -> <a download> click has none of that gap."""
    prepared = _prepare(body)
    _guard_printable(prepared)
    pdf_bytes = build_pdf(
        prepared.pages,
        layout=prepared.layout,
        export_dpi=body.export_dpi,
        **_render_kwargs(body, prepared),
    )
    filename = f"{_slugify(body.project_name or _default_pdf_basename())}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/cut-file")
def cut_file(body: PdfLayoutIn) -> Response:
    """The cut file that goes with the sheet: an SVG of every card's
    rounded trim box, in the cutter's own frame when a cutter is selected
    (for import into Silhouette Studio). With no cutter it is the trim
    boxes alone in the page frame — the mat-aligned route for a Cricut,
    which cannot scan third-party marks.

    Pure geometry — the body's entries are accepted (it is the same
    PdfLayoutIn the preview sends) but not read — except under Radius'
    Proper Alpha Cards, where a cell's corner depends on the card in it.
    Then, if the decklist has an Alpha printing in it, the deck is laid out
    as the PDF would be and the download is a ZIP of one SVG per page
    (`<deck>-cut-page-NN.svg`), since the sheets no longer share one cut.
    Decided from the decklist, not from what matched, so the client can
    name the file before asking (the save dialog comes first): the same
    test as PdfPage's deckHasAlphaCards. Any other deck gets the single
    SVG."""
    layout = _layout_from_body(body, body.offset_x_mm, body.offset_y_mm)
    registration = _registration(body)
    deck = _slugify(body.project_name or _default_pdf_basename())
    alpha_deck = layout.corner_radius is CornerRadius.ALPHA_CARDS and any(
        (e.set_code or "").lower() == ALPHA_SET_CODE for e in body.entries
    )
    if not alpha_deck:
        return Response(
            content=build_cut_file_svg(layout, registration).encode("utf-8"),
            media_type="image/svg+xml",
            headers={"Content-Disposition": f'attachment; filename="{deck}-cut.svg"'},
        )
    # Nothing matched at all still gets one sheet's file: the ZIP was
    # promised, and an empty one is no use to anybody.
    page_radii = [cell_corner_radii(page, layout) for page in _prepare(body).pages] or [
        cell_corner_radii((), layout)
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for number, radii in enumerate(page_radii, start=1):
            archive.writestr(
                f"{deck}-cut-page-{number:02d}.svg",
                build_cut_file_svg(layout, registration, radii),
            )
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{deck}-cut.zip"'},
    )


# --- Render jobs -----------------------------------------------------------
#
# The synchronous routes above stay as they are (CLI/scripted callers, and
# the existing API tests). The desktop client uses the job routes below
# instead: rendering a sheet costs ~0.7s per unique image, so a real deck
# spends tens of seconds before any bytes exist to send, and a single
# blocking POST leaves the UI with nothing to show for it. Here the render
# runs on its own thread and the client polls (completed, total).


def _run_render(job_id: str, *, prepared: PreparedRender, body: PdfLayoutIn) -> None:
    """Render thread body. Owns the job's terminal state: every exit path
    (success, cancel, failure) marks the job, or the client would poll a
    "rendering" job forever."""

    def on_progress(completed: int, _total: int) -> None:
        if pdf_jobs.is_cancel_requested(job_id):
            raise PdfRenderCanceled()
        pdf_jobs.set_progress(job_id, completed)

    try:
        pdf_bytes = build_pdf(
            prepared.pages,
            layout=prepared.layout,
            export_dpi=body.export_dpi,
            on_progress=on_progress,
            **_render_kwargs(body, prepared),
        )
        pdf_jobs.finish(job_id, pdf_bytes)
    except PdfRenderCanceled:
        pdf_jobs.mark_canceled(job_id)
    except Exception as exc:  # noqa: BLE001 — must reach the client as a status
        pdf_jobs.fail(job_id, str(exc))


@router.post("/jobs", response_model=PdfJobOut, status_code=202)
def start_pdf_job(body: PdfLayoutIn) -> PdfJobOut:
    """Start a background render and return its id.

    _prepare() runs synchronously here on purpose: it's cheap (a DB read
    plus matching) and it's what raises the 400s for an empty/unprintable
    request, so those still land on this call rather than surfacing much
    later as a failed job the user has already started waiting on.
    """
    prepared = _prepare(body)
    _guard_printable(prepared)
    # One render at a time: each finished job pins a whole PDF in memory
    # until it's fetched, and the client single-flights downloads anyway.
    if pdf_jobs.active_count() > 0:
        raise HTTPException(
            status_code=409, detail="A PDF is already being generated — wait for it to finish."
        )

    filename = f"{_slugify(body.project_name or _default_pdf_basename())}.pdf"
    job = pdf_jobs.create_job(
        filename=filename,
        total=unique_image_count(
            prepared.pages,
            back_printing=body.back_printing,
            back_images=prepared.back_images,
            default_back_hash=prepared.default_back_hash,
            reverse_fill=ReverseFill(body.reverse_fill.value),
        ),
    )
    threading.Thread(
        target=_run_render,
        args=(job.id,),
        kwargs={"prepared": prepared, "body": body},
        daemon=True,
    ).start()
    return PdfJobOut(job_id=job.id, total=job.total)


@router.get("/jobs/{job_id}", response_model=PdfJobStatusOut)
def pdf_job_status(job_id: str) -> PdfJobStatusOut:
    job = pdf_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired PDF job.")
    return PdfJobStatusOut(
        status=job.status, completed=job.completed, total=job.total, error=job.error
    )


@router.post("/jobs/{job_id}/cancel", status_code=204)
def cancel_pdf_job(job_id: str) -> Response:
    if not pdf_jobs.request_cancel(job_id):
        raise HTTPException(status_code=404, detail="Unknown or expired PDF job.")
    return Response(status_code=204)


@router.get("/jobs/{job_id}/result")
def pdf_job_result(job_id: str) -> Response:
    """Hand over a finished render's bytes, evicting the job as it goes —
    this is a plain GET so the desktop client can point Rust's downloader
    straight at it (see main.rs), keeping a large PDF out of the webview.
    """
    job = pdf_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired PDF job.")
    if job.status != pdf_jobs.DONE:
        raise HTTPException(
            status_code=409, detail=f"PDF job is not ready (status: {job.status})."
        )
    result = pdf_jobs.pop_result(job_id)
    if result is None:  # raced another fetch between the check and the pop
        raise HTTPException(status_code=404, detail="Unknown or expired PDF job.")
    filename, pdf_bytes = result
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
