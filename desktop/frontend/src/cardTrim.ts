// Where the cut lands on an image that may carry bleed: the geometry the
// full-image viewer draws its dashed trim line from, and the MPC Fill
// picker uses to crop the bleed off its tiles and outline the trim on its
// preview. In percent of the displayed image, so it survives any size.
//
// It matches the server either way: custom images are cover-cropped to
// the bled box ((63+2b):(88+2b)) on upload (proxy_scaler/customs.py), and
// back images get the same trim box from pdf_layout.py::fit_bled_image.

// Card trim size, mm — proxy_scaler/dpi.py::CARD_WIDTH_MM / CARD_HEIGHT_MM.
export const CARD_W_MM = 63;
export const CARD_H_MM = 88;
// The die-cut corner radius Scryfall renders (and a corner punch makes):
// 2.7 mm, as a horizontal/vertical percentage pair of the 63:88 trim box,
// which comes out circular at any display size.
const CORNER_RADIUS_MM = 2.7;
export const TRIM_CORNER_RADIUS = `${(CORNER_RADIUS_MM / CARD_W_MM) * 100}% / ${(CORNER_RADIUS_MM / CARD_H_MM) * 100}%`;

export interface PctRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/**
 * For an image of `natural` size declared to carry `bleedMm` per side:
 * `box`, the bled-aspect box the server keeps (the whole image unless its
 * aspect is off), and `trim`, that box inset by the bleed — the card's
 * cut edge. With no bleed the two coincide.
 */
export function trimGeometry(
  natural: { w: number; h: number } | null,
  bleedMm: number,
): { box: PctRect; trim: PctRect; cropped: boolean } {
  const b = Math.max(0, bleedMm);
  const boxAspect = (CARD_W_MM + 2 * b) / (CARD_H_MM + 2 * b);
  let box: PctRect = { left: 0, top: 0, width: 100, height: 100 };
  if (natural && natural.w > 0 && natural.h > 0) {
    const a = natural.w / natural.h;
    if (a > boxAspect) {
      const width = (boxAspect / a) * 100;
      box = { left: (100 - width) / 2, top: 0, width, height: 100 };
    } else {
      const height = (a / boxAspect) * 100;
      box = { left: 0, top: (100 - height) / 2, width: 100, height };
    }
  }
  const insetX = (b / (CARD_W_MM + 2 * b)) * box.width;
  const insetY = (b / (CARD_H_MM + 2 * b)) * box.height;
  const trim = {
    left: box.left + insetX,
    top: box.top + insetY,
    width: box.width - 2 * insetX,
    height: box.height - 2 * insetY,
  };
  const cropped =
    natural != null && (Math.abs(box.width - 100) > 0.05 || Math.abs(box.height - 100) > 0.05);
  return { box, trim, cropped };
}

export const pct = (r: PctRect) => ({
  left: `${r.left}%`,
  top: `${r.top}%`,
  width: `${r.width}%`,
  height: `${r.height}%`,
});

/** Where to draw an image inside a card-shaped frame so only its `trim`
 *  shows: the trim box scaled up to fill the frame. */
export function fillFrameWith(trim: PctRect) {
  return {
    left: `${(-trim.left / trim.width) * 100}%`,
    top: `${(-trim.top / trim.height) * 100}%`,
    width: `${(100 / trim.width) * 100}%`,
    height: `${(100 / trim.height) * 100}%`,
  };
}

