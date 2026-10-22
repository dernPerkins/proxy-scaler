// PROTOTYPE — throwaway harness for prototype-guides.html: every Guides
// variant side by side, at the sidebar column's real width.
import ReactDOM from "react-dom/client";
import "../styles.css";
import { GUIDE_VARIANTS, VariantA, VariantB, VariantC, type GuideSeed } from "./pdf-guides.prototype";

const seed: GuideSeed = {
  guide_width_pt: 0.75,
  guide_length_mm: 2.75,
  hide_card_guides_front: false,
  hide_page_guides_front: false,
  hide_card_guides_back: true,
  hide_page_guides_back: true,
  back_printing: new URLSearchParams(location.search).get("backs") !== "off",
};

const components = { A: VariantA, B: VariantB, C: VariantC };

ReactDOM.createRoot(document.getElementById("root")!).render(
  <div style={{ display: "flex", gap: 28, padding: 24, alignItems: "flex-start", flexWrap: "wrap" }}>
    {GUIDE_VARIANTS.map(({ key, name }) => {
      const V = components[key];
      return (
        <section key={key} style={{ width: 286 }}>
          {/* 286 - 2×16 padding = 254px, the real PDF sidebar column's width. */}
          <div style={{ fontSize: 11, fontWeight: 700, color: "#ff2d87", marginBottom: 6 }}>
            VARIANT {key} · {name}
          </div>
          <div className="sidebar" style={{ width: 286, background: "var(--surface)", padding: 16, borderRadius: 10, border: "1px solid var(--border)" }}>
            <V seed={seed} />
          </div>
        </section>
      );
    })}
  </div>,
);
