// A select-styled dropdown whose list is checkboxes: tick any number of
// options, or none for all of them. The printing grid's set and language
// filters. With `findLabel`, a find box filters the list (a basic land spans
// hundreds of sets); a short list (languages) goes without.
import { useEffect, useMemo, useRef, useState } from "react";

export interface FilterOption {
  value: string;
  /** What the list and the toggle show — the set or language code. */
  label: string;
  /** The full name, as a tooltip. */
  title?: string | null;
}

export default function MultiSelectFilter({
  options,
  selected,
  onChange,
  disabled,
  allLabel,
  ariaLabel,
  findLabel,
}: {
  options: FilterOption[];
  selected: ReadonlySet<string>;
  onChange: (next: ReadonlySet<string>) => void;
  disabled: boolean;
  /** The toggle's text and the clear button's, e.g. "All Sets". */
  allLabel: string;
  ariaLabel: string;
  findLabel?: string;
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
          (o) => o.label.toLowerCase().includes(q) || o.title?.toLowerCase().includes(q),
        )
      : options;
  }, [options, find]);

  const summary = useMemo(() => {
    if (selected.size === 0) return allLabel;
    const labels = options.filter((o) => selected.has(o.value)).map((o) => o.label);
    return labels.length <= 3
      ? labels.join(", ")
      : `${labels.slice(0, 2).join(", ")} +${labels.length - 2}`;
  }, [options, selected, allLabel]);

  function toggle(value: string) {
    const next = new Set(selected);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    onChange(next);
  }

  return (
    <div
      className="multi-filter"
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
        className="select-button multi-filter-toggle"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        {summary}
      </button>
      {open && (
        <div className="multi-filter-panel">
          {findLabel && (
            <input
              type="search"
              placeholder={findLabel}
              aria-label={findLabel}
              value={find}
              autoFocus
              onChange={(e) => setFind(e.target.value)}
            />
          )}
          <button
            type="button"
            className="btn-sm"
            disabled={selected.size === 0}
            onClick={() => onChange(new Set())}
          >
            {allLabel}
          </button>
          <div className="multi-filter-list" role="listbox" aria-multiselectable="true">
            {shown.map((o) => (
              <label
                key={o.value}
                className="check multi-filter-option"
                title={o.title ?? undefined}
              >
                <input
                  type="checkbox"
                  checked={selected.has(o.value)}
                  onChange={() => toggle(o.value)}
                />
                <span className="mono">{o.label}</span>
              </label>
            ))}
            {shown.length === 0 && <p className="hint">No matches.</p>}
          </div>
        </div>
      )}
    </div>
  );
}
