// PROTOTYPE — throwaway. Floating bar that cycles a page's `?variant=`
// search param, for comparing UI variants side by side in the real page.
// Dev builds only; delete once a variant has been picked.
import { useEffect, type CSSProperties } from "react";
import { useSearchParams } from "react-router-dom";

export function usePrototypeVariant(keys: readonly string[]): string | null {
  const [params] = useSearchParams();
  const v = params.get("variant");
  return v && keys.includes(v) ? v : null;
}

export default function PrototypeSwitcher({
  variants,
}: {
  variants: readonly { key: string; name: string }[];
}) {
  const [params, setParams] = useSearchParams();
  const current = params.get("variant");
  // "Off" (no param) is part of the cycle so the current UI stays one
  // click away for comparison.
  const cycle = [null, ...variants.map((v) => v.key)];
  const index = Math.max(0, cycle.indexOf(current));

  function go(step: number) {
    const next = cycle[(index + step + cycle.length) % cycle.length];
    setParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (next) n.set("variant", next);
        else n.delete("variant");
        return n;
      },
      { replace: true },
    );
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const t = e.target as HTMLElement | null;
      if (t && (t.closest("input, textarea, select, [contenteditable]") || t.isContentEditable)) return;
      go(e.key === "ArrowLeft" ? -1 : 1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!import.meta.env.DEV) return null;

  const meta = variants.find((v) => v.key === current);
  const label = meta ? `${meta.key} (${meta.name})` : "Current UI";
  const btn: CSSProperties = {
    background: "transparent",
    border: "none",
    color: "#fff",
    fontSize: 16,
    padding: "2px 10px",
    cursor: "pointer",
  };

  return (
    <div
      style={{
        position: "fixed",
        bottom: 18,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 10000,
        display: "flex",
        alignItems: "center",
        gap: 4,
        padding: "6px 8px",
        borderRadius: 999,
        background: "#ff2d87",
        color: "#fff",
        fontSize: 13,
        fontWeight: 600,
        boxShadow: "0 6px 20px rgba(0,0,0,0.35)",
      }}
    >
      <button type="button" style={btn} onClick={() => go(-1)} aria-label="Previous variant">
        ‹
      </button>
      <span style={{ minWidth: 190, textAlign: "center" }}>Prototype: {label}</span>
      <button type="button" style={btn} onClick={() => go(1)} aria-label="Next variant">
        ›
      </button>
    </div>
  );
}
