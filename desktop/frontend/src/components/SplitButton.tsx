import { useEffect, useRef, useState, type ReactNode } from "react";

export interface SplitButtonOption {
  key: string;
  label: string;
}

// A primary action with a caret listing related ones: [Action][▾]. Open/
// close follows CompareMenu's pattern (outside mousedown / Esc), and the
// menu reuses its look.
export default function SplitButton({
  label,
  onClick,
  options,
  onPick,
  disabled,
  title,
  primary,
}: {
  label: ReactNode;
  onClick: () => void;
  options: SplitButtonOption[];
  onPick: (key: string) => void;
  disabled?: boolean;
  title?: string;
  primary?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    function onMouseDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
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
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const cls = primary ? "btn-primary" : undefined;
  return (
    <span className="split-button" ref={wrapRef}>
      <button className={cls} onClick={onClick} disabled={disabled} title={title}>
        {label}
      </button>
      <button
        className={`${cls ?? ""} split-caret`.trim()}
        aria-label="More upscale options"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        ▾
      </button>
      {open && (
        <div className="compare-menu split-menu" role="menu">
          {options.map((option) => (
            <button
              key={option.key}
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onPick(option.key);
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}
