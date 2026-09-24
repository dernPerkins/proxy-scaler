import { ReactCompareSlider, ReactCompareSliderImage } from "react-compare-slider";

import type { CompareSide } from "./CompareMenu";
import ModalOverlay from "./ModalOverlay";

interface CompareDialogProps {
  cardName: string;
  // The variant whose Compare was clicked: fixed, always on the right.
  mine: CompareSide;
  // What it's compared against: the left side, switchable in the header.
  against: CompareSide;
  // Every other version of the same face (never `mine`).
  options: CompareSide[];
  onChangeAgainst: (side: CompareSide) => void;
  onClose: () => void;
}

// Ports ui/compare.py's modal image-comparison slider — the old Streamlit
// version embedded a custom JS/CSS slider via components.v1.html inside an
// iframe; this renders react-compare-slider directly in the DOM instead,
// no iframe indirection (one less place for the WKWebView-class of gaps
// to hide). Any two versions of a face can be compared, across models and
// DPIs: both images render object-fit: contain in the same box, so a
// 1200 DPI and an 800 DPI file line up without special handling (the
// original-vs-1200 DPI pair was already a 4x size difference).
export default function CompareDialog({
  cardName,
  mine,
  against,
  options,
  onChangeAgainst,
  onClose,
}: CompareDialogProps) {
  return (
    <ModalOverlay onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">
            {cardName} — {against.label} vs {mine.label}
          </span>
          <span className="compare-head-controls">
            {options.length > 1 && (
              <label className="compare-against">
                <span>Compare against</span>
                <select
                  value={against.key}
                  onChange={(e) => {
                    const side = options.find((o) => o.key === e.target.value);
                    if (side) onChangeAgainst(side);
                  }}
                >
                  {options.map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <button onClick={onClose}>Close</button>
          </span>
        </div>
        {/* Explicit height, deliberately a bit under the modal's own 90vh
            cap so the header and padding still fit — the slider needs a
            definite height to size itself, and an earlier version at full
            natural image height overflowed the window entirely. */}
        <div className="compare-stage">
          <ReactCompareSlider
            style={{
              height: "min(76vh, 900px)",
              width: "100%",
            }}
            itemOne={
              <ReactCompareSliderImage
                key={against.key}
                src={against.url}
                alt={against.label}
                style={{ objectFit: "contain" }}
              />
            }
            itemTwo={
              <ReactCompareSliderImage
                src={mine.url}
                alt={mine.label}
                style={{ objectFit: "contain" }}
              />
            }
          />
          {/* Which side is which, once the left one can change. */}
          <span className="compare-side-label compare-side-left">{against.label}</span>
          <span className="compare-side-label compare-side-right">{mine.label}</span>
        </div>
      </div>
    </ModalOverlay>
  );
}
