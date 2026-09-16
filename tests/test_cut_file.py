from xml.etree import ElementTree as ET

from proxy_scaler.cut_file import SVG_NS, build_cut_file_svg, card_trim_rects
from proxy_scaler.dpi import CARD_HEIGHT_MM, CARD_WIDTH_MM
from proxy_scaler.pdf_layout import (
    CARD_CORNER_RADIUS_MM,
    REG_ARM_MM,
    REG_SQUARE_MM,
    REG_THICKNESS_MM,
    CutterMarkStyle,
    CutterOrientation,
    RegistrationMarks,
    resolve_page_layout,
)


def _a4(cols: int = 2, rows: int = 2, **overrides):
    kwargs = dict(page_w_mm=210.0, page_h_mm=297.0, cols=cols, rows=rows)
    kwargs.update(overrides)
    return resolve_page_layout(**kwargs)


def _rects(svg: str, group_id: str) -> list[dict[str, float]]:
    root = ET.fromstring(svg)
    [group] = [g for g in root.findall(f"{{{SVG_NS}}}g") if g.get("id") == group_id]
    out = []
    for rect in group.findall(f"{{{SVG_NS}}}rect"):
        out.append({k: float(v) for k, v in rect.attrib.items() if k in ("x", "y", "width", "height", "rx")})
        out[-1]["fill"] = rect.get("fill")  # type: ignore[assignment]
    return out


def _has(rects: list[dict], x: float, y: float, w: float, h: float) -> bool:
    return any(
        abs(r["x"] - x) < 1e-3 and abs(r["y"] - y) < 1e-3 and abs(r["width"] - w) < 1e-3 and abs(r["height"] - h) < 1e-3
        for r in rects
    )


def test_cut_file_is_page_sized_svg_in_mm() -> None:
    svg = build_cut_file_svg(_a4(), RegistrationMarks())
    assert svg.startswith('<?xml version="1.0"')
    root = ET.fromstring(svg)
    assert root.tag == f"{{{SVG_NS}}}svg"
    assert root.get("width") == "210.000mm"
    assert root.get("height") == "297.000mm"
    assert root.get("viewBox") == "0 0 210.000 297.000"


def test_cut_file_marks_match_the_printed_marks() -> None:
    a, t = REG_ARM_MM, REG_THICKNESS_MM
    marks = _rects(build_cut_file_svg(_a4(), RegistrationMarks(inset_mm=10.0)), "registration-marks")
    assert len(marks) == 5
    assert all(m["fill"] == "#000" for m in marks)
    assert _has(marks, 10, 10, REG_SQUARE_MM, REG_SQUARE_MM)
    assert _has(marks, 210 - 10 - a, 10, a, t)
    assert _has(marks, 10, 297 - 10 - a, t, a)
    four = _rects(
        build_cut_file_svg(_a4(), RegistrationMarks(style=CutterMarkStyle.FOUR_POINT, inset_mm=10.0)),
        "registration-marks",
    )
    assert len(four) == 7
    assert _has(four, 210 - 10 - a, 297 - 10 - t, a, t)


def test_cut_file_has_one_rounded_trim_box_per_grid_cell() -> None:
    layout = _a4(cols=2, rows=2)
    cuts = _rects(build_cut_file_svg(layout, RegistrationMarks()), "cut-lines")
    assert len(cuts) == 4
    assert all(c["fill"] == "none" for c in cuts)
    assert all(abs(c["rx"] - CARD_CORNER_RADIUS_MM) < 1e-6 for c in cuts)
    x0 = layout.margin_x_mm + layout.bleed_mm
    y0 = layout.margin_y_mm + layout.bleed_mm
    assert _has(cuts, x0, y0, CARD_WIDTH_MM, CARD_HEIGHT_MM)
    assert _has(cuts, x0 + layout.cell_w_mm, y0 + layout.cell_h_mm, CARD_WIDTH_MM, CARD_HEIGHT_MM)
    # The full grid, occupied or not — it's a per-sheet template.
    assert len(card_trim_rects(_a4(cols=3, rows=3))) == 9


def test_cut_file_is_emitted_in_the_cutter_frame_when_rotated() -> None:
    """Landscape marks on a portrait page: the file is the landscape sheet
    the cutter sees, square top-left, and each card box rotated with it
    (page bottom-left corner -> cutter top-left)."""
    layout = _a4(cols=2, rows=2)
    svg = build_cut_file_svg(layout, RegistrationMarks(orientation=CutterOrientation.LANDSCAPE, inset_mm=10.0))
    root = ET.fromstring(svg)
    assert (root.get("width"), root.get("height")) == ("297.000mm", "210.000mm")
    marks = _rects(svg, "registration-marks")
    assert _has(marks, 10, 10, REG_SQUARE_MM, REG_SQUARE_MM)
    cuts = _rects(svg, "cut-lines")
    assert len(cuts) == 4
    assert all(abs(c["width"] - CARD_HEIGHT_MM) < 1e-3 and abs(c["height"] - CARD_WIDTH_MM) < 1e-3 for c in cuts)
    # Page cell (0,0) at (x0, y0) lands at cutter (297 - y0 - 88, x0).
    x0 = layout.margin_x_mm + layout.bleed_mm
    y0 = layout.margin_y_mm + layout.bleed_mm
    assert _has(cuts, 297 - y0 - CARD_HEIGHT_MM, x0, CARD_HEIGHT_MM, CARD_WIDTH_MM)
