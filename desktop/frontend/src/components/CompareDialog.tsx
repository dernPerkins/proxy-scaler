import { ReactCompareSlider, ReactCompareSliderImage } from "react-compare-slider";

import ModalOverlay from "./ModalOverlay";

interface CompareDialogProps {
  originalUrl: string;
  upscaledUrl: string;
  label: string;
  onClose: () => void;
}

// Ports ui/compare.py's modal image-comparison slider — the old Streamlit
// version embedded a custom JS/CSS slider via components.v1.html inside an
// iframe; this renders react-compare-slider directly in the DOM instead,
// no iframe indirection (one less place for the WKWebView-class of gaps
// to hide).
export default function CompareDialog({
  originalUrl,
  upscaledUrl,
  label,
  onClose,
}: CompareDialogProps) {
  return (
    <ModalOverlay onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">{label}</span>
          <button onClick={onClose}>Close</button>
        </div>
        {/* Explicit height, deliberately a bit under the modal's own 90vh
            cap so the header and padding still fit — the slider needs a
            definite height to size itself, and an earlier version at full
            natural image height overflowed the window entirely. */}
        <ReactCompareSlider
          style={{
            height: "min(76vh, 900px)",
            width: "100%",
          }}
          itemOne={
            <ReactCompareSliderImage
              src={originalUrl}
              alt="Original"
              style={{ objectFit: "contain" }}
            />
          }
          itemTwo={
            <ReactCompareSliderImage
              src={upscaledUrl}
              alt="Upscaled"
              style={{ objectFit: "contain" }}
            />
          }
        />
      </div>
    </ModalOverlay>
  );
}
