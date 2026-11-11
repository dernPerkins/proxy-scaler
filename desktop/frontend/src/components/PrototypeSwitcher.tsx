// PROTOTYPE — throwaway. A floating bar that flips a `?variant=` search param
// between UI variants on the page it is mounted on (← / → keys cycle too,
// except while a tile has focus: there the arrows move a picked-up card).
// Dev builds only: a stray merge can't ship it. Delete once a variant wins.
import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

export interface PrototypeVariant {
  key: string;
  name: string;
}

/** The current variant key from `?<param>=`, defaulting to the first. */
export function usePrototypeVariant(
  variants: readonly PrototypeVariant[],
  param = "variant",
): string {
  const [params] = useSearchParams();
  const v = params.get(param);
  return variants.some((x) => x.key === v) ? v! : variants[0].key;
}

export default function PrototypeSwitcher({
  variants,
  param = "variant",
  label,
}: {
  variants: readonly PrototypeVariant[];
  param?: string;
  /** What is being prototyped, shown before the variant name. */
  label?: string;
}) {
  const [params, setParams] = useSearchParams();
  const current = usePrototypeVariant(variants, param);
  const i = variants.findIndex((v) => v.key === current);

  const go = (step: number) => {
    const next = variants[(i + step + variants.length) % variants.length];
    const p = new URLSearchParams(params);
    p.set(param, next.key);
    setParams(p, { replace: true });
  };

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const onKey = (e: KeyboardEvent) => {
      if (
        e.defaultPrevented ||
        (e.key !== "ArrowLeft" && e.key !== "ArrowRight")
      )
        return;
      const t = e.target as HTMLElement | null;
      if (
        t?.closest(
          "input, textarea, select, [contenteditable='true'], [role='dialog'], .deck-cell",
        )
      )
        return;
      e.preventDefault();
      go(e.key === "ArrowLeft" ? -1 : 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!import.meta.env.DEV) return null;
  return (
    <div
      className="proto-switcher"
      role="toolbar"
      aria-label="Prototype variants"
    >
      <button
        type="button"
        onClick={() => go(-1)}
        aria-label="Previous variant"
      >
        ←
      </button>
      <span className="proto-switcher-label">
        {label ? <span className="proto-switcher-what">{label} · </span> : null}
        {variants[i].key} ({variants[i].name})
      </span>
      <button type="button" onClick={() => go(1)} aria-label="Next variant">
        →
      </button>
    </div>
  );
}
