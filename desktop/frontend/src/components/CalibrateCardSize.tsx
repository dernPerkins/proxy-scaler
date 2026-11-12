// Calibrate, from the Grid view's zoom dock (ADR 0010): match the Actual size
// to a real card held against the screen, for a display whose detected size
// is wrong (some external monitors) or missing (Windows, Linux). The outline
// is a card at the candidate size; nudge it until the real card's edges line
// up, then Save. Saved per display, so a laptop and its external monitor each
// keep their own.
import { useEffect, useState } from "react";
import { CARD_H_MM, CARD_W_MM, type GridCardSize } from "../gridZoom";
import ModalOverlay from "./ModalOverlay";

/** One − / + press: fine enough to land within a fraction of a mm. */
const NUDGE = 0.02;
const MIN_PX_PER_MM = 2;
const MAX_PX_PER_MM = 10;

export default function CalibrateCardSize({
  scale,
  onClose,
}: {
  scale: GridCardSize["scale"];
  onClose: () => void;
}) {
  const [px, setPx] = useState(scale.pxPerMm);
  const clamp = (v: number) => Math.min(MAX_PX_PER_MM, Math.max(MIN_PX_PER_MM, v));
  const mm = scale.displayMm;
  const reported = mm ? `${Math.round(mm.w)} × ${Math.round(mm.h)} mm` : null;
  const sourceNote =
    scale.source === "calibrated"
      ? `Now: Calibrated for this display at ${scale.pxPerMm.toFixed(2)} px/mm` +
        (scale.detected && reported ? ` (it reports ${reported}, ${scale.detected.toFixed(2)} px/mm)` : "")
      : scale.source === "detected"
        ? `Now: Detected from this display, ${reported} at ${scale.pxPerMm.toFixed(2)} px/mm`
        : `Now: Estimated at ${scale.pxPerMm.toFixed(2)} px/mm (this display doesn't report its size)`;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <ModalOverlay onClick={onClose}>
      <div className="modal calibrate-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Calibrate Card Size</span>
        </div>
        <p className="calibrate-help">
          Hold a real card against the screen inside the outline, and adjust until its edges line up.
        </p>
        <p className="calibrate-source">{sourceNote}</p>
        <div className="calibrate-body">
          <div className="calibrate-card" style={{ width: CARD_W_MM * px, height: CARD_H_MM * px }}>
            <span className="calibrate-card-size mono">
              {CARD_W_MM} × {CARD_H_MM} mm
            </span>
            <span className="calibrate-card-px mono">
              {Math.round(CARD_W_MM * px)} × {Math.round(CARD_H_MM * px)} px
            </span>
          </div>
        </div>
        <div className="calibrate-adjust">
          <button type="button" aria-label="Smaller" onClick={() => setPx((v) => clamp(v - NUDGE))}>
            −
          </button>
          <input
            type="range"
            aria-label="Card size"
            min={MIN_PX_PER_MM}
            max={MAX_PX_PER_MM}
            step={0.01}
            value={px}
            onChange={(e) => setPx(Number(e.target.value))}
          />
          <button type="button" aria-label="Larger" onClick={() => setPx((v) => clamp(v + NUDGE))}>
            +
          </button>
          <span className="calibrate-value mono">{px.toFixed(2)} px/mm</span>
        </div>
        <div className="modal-actions">
          {scale.isCalibrated && (
            <button
              type="button"
              className="calibrate-reset"
              title={scale.detected ? "Use the size this display reports" : "Clear the calibration"}
              onClick={() => {
                scale.resetCalibration();
                onClose();
              }}
            >
              {scale.detected ? "Use Detected Size" : "Clear Calibration"}
            </button>
          )}
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            autoFocus
            onClick={() => {
              scale.calibrate(px);
              onClose();
            }}
          >
            Save
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
