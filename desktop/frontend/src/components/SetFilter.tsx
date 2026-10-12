// The printing grid's set filter: a dropdown of the set codes the card has
// printings in, any number of which can be ticked. Nothing ticked means
// every set. Set names ride along as tooltips; the list itself is codes,
// alphabetical, with a find box because a basic land spans hundreds.
import { useEffect, useMemo, useRef, useState } from "react";

export interface SetOption {
  code: string;
  name: string | null;
}

function summary(selected: ReadonlySet<string>): string {
  if (selected.size === 0) return "All Sets";
  const codes = [...selected].sort().map((c) => c.toUpperCase());
  return codes.length <= 3 ? codes.join(", ") : `${codes.slice(0, 2).join(", ")} +${codes.length - 2}`;
}

export default function SetFilter({
  options,
  selected,
  onChange,
  disabled,
}: {
  options: SetOption[];
  selected: ReadonlySet<string>;
  onChange: (next: ReadonlySet<string>) => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [find, setFind] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  // Outside clicks close it, like any dropdown.
  useEffect(() => {
    if (!open) return;
    function onMouseDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [open]);

  const shown = useMemo(() => {
    const q = find.trim().toLowerCase();
    return q
      ? options.filter(
          (o) => o.code.toLowerCase().includes(q) || o.name?.toLowerCase().includes(q),
        )
      : options;
  }, [options, find]);

  function toggle(code: string) {
    const next = new Set(selected);
    if (next.has(code)) next.delete(code);
    else next.add(code);
    onChange(next);
  }

  return (
    <div
      className="set-filter"
      ref={rootRef}
      onKeyDown={(e) => {
        if (e.key !== "Escape" || !open) return;
        // Close the dropdown, not the whole grid (whose Esc listener
        // skips handled events).
        e.preventDefault();
        setOpen(false);
      }}
    >
      <button
        type="button"
        className="set-filter-toggle"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="mono">{summary(selected)}</span> ▾
      </button>
      {open && (
        <div className="set-filter-panel">
          <input
            type="search"
            placeholder="Find a set"
            aria-label="Find a set"
            value={find}
            autoFocus
            onChange={(e) => setFind(e.target.value)}
          />
          <button
            type="button"
            className="btn-sm"
            disabled={selected.size === 0}
            onClick={() => onChange(new Set())}
          >
            All Sets
          </button>
          <div className="set-filter-list" role="listbox" aria-multiselectable="true">
            {shown.map((o) => (
              <label key={o.code} className="check set-filter-option" title={o.name ?? undefined}>
                <input
                  type="checkbox"
                  checked={selected.has(o.code)}
                  onChange={() => toggle(o.code)}
                />
                <span className="mono">{o.code.toUpperCase()}</span>
              </label>
            ))}
            {shown.length === 0 && <p className="hint">No sets match.</p>}
          </div>
        </div>
      )}
    </div>
  );
}
