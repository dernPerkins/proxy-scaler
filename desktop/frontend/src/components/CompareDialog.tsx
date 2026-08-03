import { ReactCompareSlider, ReactCompareSliderImage } from "react-compare-slider";

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
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0, 0, 0, 0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        style={{
          maxWidth: "90vw",
          maxHeight: "90vh",
          background: "#1b1d22",
          padding: 16,
          borderRadius: 8,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 8,
            color: "#e8e8ec",
          }}
        >
          <strong>{label}</strong>
          <button onClick={onClose}>Close</button>
        </div>
        <div style={{ maxHeight: "80vh", maxWidth: "85vw" }}>
          <ReactCompareSlider
            itemOne={<ReactCompareSliderImage src={originalUrl} alt="Original" />}
            itemTwo={<ReactCompareSliderImage src={upscaledUrl} alt="Upscaled" />}
          />
        </div>
      </div>
    </div>
  );
}
