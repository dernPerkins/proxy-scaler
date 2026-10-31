// A plain full-size view of one image: the Decklist's "View" on a card's
// Original. Unlike components/ImageViewer there is no trim/crop overlay or
// settings sidebar — there is nothing to change while looking at it. Art
// declared to carry bleed (custom and MPC Fill) shows as cut instead, the
// bleed cropped off as the MPC Fill picker's preview does. The image is a
// server URL the <img> loads directly, so no query either.
//
// Styled as the change-printing picker's preview pane: the card on a
// blurred wash of its own art, dark in both themes, frosted-glass
// controls. The frame clips to the card's die-cut corners, so a Scryfall
// PNG's transparent corners don't show as notches.
import { useEffect, useState } from "react";
import { fillFrameWith, isAlphaPrinting, trimGeometry } from "../cardTrim";
import ModalOverlay from "./ModalOverlay";

export default function FullImageDialog({
  title,
  src,
  setCode,
  bleedMm = 0,
  onClose,
}: {
  title: string;
  src: string;
  /** The printing's set, when it is a card's: Alpha's rounder corner
   *  clips wider (cardTrim.ts::isAlphaPrinting). */
  setCode?: string | null;
  /** The bleed the image carries per side, cropped off for display. */
  bleedMm?: number;
  onClose: () => void;
}) {
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [failed, setFailed] = useState(false);
  // A trimmed frame is card-shaped and sized here rather than by the
  // image: no taller than the trim box's own pixels, nor than the stage.
  const trim = natural && bleedMm > 0 ? trimGeometry(natural, bleedMm).trim : null;
  const trimmedFrame = trim
    ? {
        height: `min(94vh - 110px, 1040px, ${(natural!.h * trim.height) / 100}px, (96vw - 56px) * 88 / 63)`,
      }
    : undefined;

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <ModalOverlay onClick={onClose}>
      <div className="modal full-image-modal" onClick={(e) => e.stopPropagation()}>
        <div className="printing-preview-wash" style={{ backgroundImage: `url("${src}")` }} />
        <div className="modal-head full-image-head">
          <span className="modal-title">{title}</span>
          {natural ? (
            <span className="full-image-dims">
              {natural.w}×{natural.h} px
            </span>
          ) : null}
          <button type="button" className="printing-preview-glass full-image-close" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="full-image-stage">
          {/* Card-sized while loading so the dialog doesn't jump; then it
              wraps the image exactly, whatever its proportions. */}
          <div
            className={`full-image-frame${natural ? "" : " loading"}${trim ? " trimmed" : ""}${
              isAlphaPrinting(setCode) ? " alpha-corners" : ""
            }`}
            style={trimmedFrame}
          >
            <img
              className={natural ? "loaded" : undefined}
              style={trim ? fillFrameWith(trim) : undefined}
              src={src}
              alt={title}
              draggable={false}
              onLoad={(e) =>
                setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
              }
              onError={() => setFailed(true)}
            />
            {natural ? null : (
              <span className="printing-preview-status">
                {failed ? "Couldn't load this image." : "Loading full size…"}
              </span>
            )}
          </div>
        </div>
      </div>
    </ModalOverlay>
  );
}
