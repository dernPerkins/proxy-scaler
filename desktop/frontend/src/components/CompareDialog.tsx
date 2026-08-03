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
          width: "min(85vw, 700px)",
          maxHeight: "90vh",
          background: "#1b1d22",
          padding: 16,
          borderRadius: 8,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          boxSizing: "border-box",
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
            flexShrink: 0,
          }}
        >
          <strong>{label}</strong>
          <button onClick={onClose}>Close</button>
        </div>
        <ReactCompareSlider
          style={{
            height: "min(80vh, 977px)",
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
    </div>
  );
}
