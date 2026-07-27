"""Full-size before/after comparison modal (img-comparison-slider)."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from PIL import Image

from proxy_scaler.pipeline import FaceResult

# Large enough to inspect detail; modal/iframe scroll to see the rest.
_COMPARE_MAX_W = 900
_COMPARE_MAX_H = 1260  # ~5:7 at 900 wide


def _load_image(path: Path) -> Image.Image | None:
    """Load preserving real alpha (transparent corners) instead of flattening."""
    if not path.is_file():
        return None
    try:
        with Image.open(path) as im:
            if im.mode in ("RGBA", "LA"):
                return im.convert("RGBA")
            return im.convert("RGB")
    except OSError:
        return None


def _fit_size(size: tuple[int, int], max_w: int, max_h: int) -> tuple[int, int]:
    w, h = size
    if w <= 0 or h <= 0:
        return max_w, max_h
    scale = min(max_w / w, max_h / h, 1.0)
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def _to_data_uri(image: Image.Image) -> str:
    buf = io.BytesIO()
    if image.mode == "RGBA":
        image.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    else:
        image.save(buf, format="JPEG", quality=90, optimize=True)
        mime = "image/jpeg"
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _comparison_pair(
    original_path: Path,
    upscaled_path: Path,
    *,
    max_w: int = _COMPARE_MAX_W,
    max_h: int = _COMPARE_MAX_H,
) -> tuple[str, str, tuple[int, int]] | None:
    """Build matching-size previews so the slider lines up pixel-perfect.

    Scryfall PNGs are often 744×1040 while our DPI targets are exact 5:7
    (e.g. 2000×2800). Displaying those at width:100% causes a ~1px vertical
    drift; resizing both to the same box removes that.
    """
    original = _load_image(original_path)
    upscaled = _load_image(upscaled_path)
    if original is None or upscaled is None:
        return None

    # Prefer the upscaled aspect (canonical print size) as the comparison frame.
    target = _fit_size(upscaled.size, max_w, max_h)
    before = original.resize(target, Image.Resampling.LANCZOS)
    after = upscaled.resize(target, Image.Resampling.LANCZOS)
    return _to_data_uri(before), _to_data_uri(after), target


def render_comparison_slider(
    before_uri: str,
    after_uri: str,
    *,
    image_width: int,
    image_height: int,
) -> None:
    """Embed sneas/img-comparison-slider via an HTML component."""
    label_h = 22
    content_h = image_height + label_h + 8
    # Viewport-sized iframe — scroll inside to see the rest of a tall card.
    viewport_h = min(content_h, 640)
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <link
    rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/img-comparison-slider@8.0.6/dist/styles.css"
  />
  <script
    defer
    src="https://cdn.jsdelivr.net/npm/img-comparison-slider@8.0.6/dist/index.js"
  ></script>
  <style>
    html, body {{
      margin: 0;
      padding: 0;
      background: transparent;
    }}
    .wrap {{
      width: 100%;
      box-sizing: border-box;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding-bottom: 8px;
    }}
    .labels {{
      flex: 0 0 {label_h}px;
      width: {image_width}px;
      max-width: 100%;
      display: flex;
      justify-content: space-between;
      align-items: center;
      color: #a3a8b4;
      font: 600 11px/1 system-ui, sans-serif;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      box-sizing: border-box;
      padding: 0 2px;
    }}
    img-comparison-slider {{
      flex: 0 0 {image_height}px;
      width: {image_width}px;
      max-width: 100%;
      height: {image_height}px;
      outline: none;
      --divider-width: 2px;
      --divider-color: #fafafa;
      --default-handle-width: 40px;
      border-radius: 6px;
      overflow: hidden;
      display: block;
      line-height: 0;
      font-size: 0;
    }}
    img-comparison-slider img {{
      width: {image_width}px;
      height: {image_height}px;
      max-width: 100%;
      display: block;
      margin: 0;
      padding: 0;
      border: 0;
      object-fit: fill;
      object-position: 0 0;
      -webkit-user-drag: none;
      user-select: none;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="labels"><span>Original</span><span>Upscaled</span></div>
    <img-comparison-slider>
      <img slot="first" alt="Original" width="{image_width}" height="{image_height}"
           src="{before_uri}" />
      <img slot="second" alt="Upscaled" width="{image_width}" height="{image_height}"
           src="{after_uri}" />
    </img-comparison-slider>
  </div>
</body>
</html>
"""
    components.html(html, height=viewport_h, scrolling=True)


@st.dialog("Image comparison", width="large")
def open_comparison_dialog(item: FaceResult) -> None:
    """Modal: original vs upscaled with a drag slider (img-comparison-slider)."""
    label = item.face_name
    if item.face_label:
        label = f"{label} ({item.face_label})"
    device = (item.device or "unknown").lower()
    device_bit = {"gpu": "GPU", "cpu": "CPU"}.get(device, "device?")
    st.markdown(
        f"**{label}** — `{item.set_code.upper()}/{item.collector_number}` · "
        f"**{item.model}** · **{item.dpi} DPI** · **{device_bit}**"
    )
    st.caption(
        "Drag the handle (or use ← →) to compare. Scroll inside the frame "
        "to see the rest of the card."
    )

    has_original = item.original_path.is_file()
    has_upscaled = item.out_path.is_file()

    if not has_original and not has_upscaled:
        st.error("Both images are missing on disk.")
        return
    if not has_original:
        st.warning("Original image missing — showing upscaled only.")
        st.image(str(item.out_path), use_container_width=True)
        return
    if not has_upscaled:
        st.warning("Upscaled image missing — showing original only.")
        st.image(str(item.original_path), use_container_width=True)
        return

    pair = _comparison_pair(item.original_path, item.out_path)
    if pair is None:
        st.error("Could not load images for comparison.")
        return

    before_uri, after_uri, (w, h) = pair
    render_comparison_slider(
        before_uri,
        after_uri,
        image_width=w,
        image_height=h,
    )
    st.caption(
        f"Comparison view {w}×{h} (both layers matched) · `{item.out_path.name}`"
    )
