import { useEffect, useRef, useState } from "react";

// One thing a compare can show: the face's original, or one of its
// upscaled variants (any model, any DPI). `key` is stable per face
// ("original" | "<dpi>:<model>").
export interface CompareSide {
  key: string;
  label: string;
  url: string;
}

// The caret half of a Compare split button: a popover listing every other
// version of the same face, so a variant can be compared against a
// different model or DPI in one click instead of Original-then-switch.
// Open/close follows PrintingPicker's pattern (outside mousedown / Esc).
export default function CompareMenu({
  options,
  onPick,
}: {
  options: CompareSide[];
  onPick: (side: CompareSide) => void;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    function onMouseDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <span className="compare-menu-anchor" ref={wrapRef}>
      <button
        className="btn-sm compare-caret"
        title="Compare against another version"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        ▾
      </button>
      {open && (
        <div className="compare-menu" role="menu">
          <div className="compare-menu-head">Compare against</div>
          {options.map((side) => (
            <button
              key={side.key}
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onPick(side);
              }}
            >
              {side.label}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}
