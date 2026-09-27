// The full-image viewer shared by the Backs and Customs tabs: the whole
// upload with the trim line drawn over it from the image's bleed
// declaration, beside that page's own settings so the declaration can be
// changed while looking at the file.
//
// A 168px thumbnail cannot show whether a file already carries bleed, and
// that is exactly the question both tabs' checkbox asks. One geometry
// serves both, because it matches the server either way: custom images are
// cover-cropped to the bled box on upload (proxy_scaler/customs.py), and
// back images get the same trim box from pdf_layout.py::fit_bled_image —
// whatever the project's own bleed.
import { useEffect, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import ModalOverlay from "./ModalOverlay";

// Mirrors proxy_scaler/dpi.py::MAX_BLEED_MM.
export const MAX_BLEED_MM = 10;
// Card trim size, mm — proxy_scaler/dpi.py::CARD_WIDTH_MM / CARD_HEIGHT_MM.
const CARD_W_MM = 63;
const CARD_H_MM = 88;
// The die-cut corner radius Scryfall renders (and a corner punch makes):
// 2.7 mm. Drawn on the trim line as a horizontal/vertical percentage pair
// of the 63:88 trim box, which comes out circular at any display size.
const CORNER_RADIUS_MM = 2.7;
const TRIM_CORNER_RADIUS = `${(CORNER_RADIUS_MM / CARD_W_MM) * 100}% / ${(CORNER_RADIUS_MM / CARD_H_MM) * 100}%`;

/** A magnifier with a plus: "zoom in / view at full size". Not a bare
 *  plus, which on a tile that also has "Add to project" would read as add. */
export function MagnifierIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3M11 8v6M8 11h6" />
    </svg>
  );
}

export default function ImageViewer({
  title,
  fullQueryKey,
  loadFull,
  includesBleed,
  bleedMm,
  width,
  height,
  sourceDpi,
  onClose,
  children,
}: {
  title: string;
  fullQueryKey: readonly unknown[];
  loadFull: () => Promise<string | null>;
  includesBleed: boolean;
  bleedMm: number;
  width: number;
  height: number;
  sourceDpi: number;
  onClose: () => void;
  /** The page's settings fields, shown beside the image. */
  children: ReactNode;
}) {
  const fullQuery = useQuery({
    queryKey: fullQueryKey,
    queryFn: loadFull,
    staleTime: Infinity,
  });
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  // Geometry, in percent of the displayed image so it survives any
  // resize. The upload is cover-cropped to the bled aspect
  // ((63+2b):(88+2b)) about its centre — that box is the crop outline;
  // inset by the declared bleed on each side it is the trim line. With
  // no declaration b is 0 and the two coincide: the file's edge is the
  // card's edge and bleed will be generated outside it.
  const b = includesBleed ? bleedMm : 0;
  const boxAspect = (CARD_W_MM + 2 * b) / (CARD_H_MM + 2 * b);
  let box = { left: 0, top: 0, width: 100, height: 100 };
  if (natural) {
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
  const pct = (r: { left: number; top: number; width: number; height: number }) => ({
    left: `${r.left}%`,
    top: `${r.top}%`,
    width: `${r.width}%`,
    height: `${r.height}%`,
  });
  const cropped =
    natural != null && (Math.abs(box.width - 100) > 0.05 || Math.abs(box.height - 100) > 0.05);

  return (
    <ModalOverlay onClick={onClose}>
      <div className="modal viewer-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">{title}</span>
          <button type="button" className="ghost" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="viewer-body">
          <div className="viewer-stage">
            {fullQuery.data ? (
              <div className="viewer-frame">
                <img
                  src={fullQuery.data}
                  alt={title}
                  onLoad={(e) =>
                    setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
                  }
                />
                {natural ? (
                  <>
                    {cropped ? <div className="viewer-crop" style={pct(box)} /> : null}
                    <div
                      className="viewer-trim"
                      style={{ ...pct(trim), borderRadius: TRIM_CORNER_RADIUS }}
                    />
                  </>
                ) : null}
              </div>
            ) : (
              <p className="hint" style={{ padding: 24 }}>
                {fullQuery.isError ? "Couldn't load this image." : "Loading…"}
              </p>
            )}
          </div>
          <aside className="viewer-settings">
            <p className="hint" style={{ marginBottom: 12 }}>
              {includesBleed
                ? `Dashed line: the trim edge, ${bleedMm} mm inside the file's edge. Everything outside it is the bleed the file already carries.`
                : "Dashed line: the card's edge. Bleed is generated outside it when printing."}
              {cropped
                ? " Shaded: cropped off to fit the card's proportions."
                : null}
            </p>
            <p className="hint" style={{ marginBottom: 12 }}>
              {width}×{height} px · {Math.round(sourceDpi)} DPI at card size
            </p>
            {children}
          </aside>
        </div>
      </div>
    </ModalOverlay>
  );
}
