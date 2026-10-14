// A select-styled dropdown whose list is checkboxes: tick any number of
// options, or none for all of them. The printing grid's set and language
// filters. With `findLabel`, a find box filters the list (a basic land spans
// hundreds of sets); a short list (languages) goes without. A `preset` is a
// named selection that isn't a set of ticks (the language filter's
// "EN + Exclusives"); ticking anything or choosing All leaves it.
//
// The panel is portalled to <body> and placed like SelectMenu's list
// (useDropdownPosition): hung from the toggle's right edge, flipped above
// when there's no room below, never clipped by the printing grid's modal.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useDropdownPosition } from "../useDropdownPosition";

export interface FilterOption {
  value: string;
  /** What the list and the toggle show — the set or language code. */
  label: string;
  /** The full name, as a tooltip. */
  title?: string | null;
  /** Shown before the label in the list — the set filter's set symbol. */
  icon?: ReactNode;
}

export default function MultiSelectFilter({
  options,
  selected,
  onChange,
  disabled,
  allLabel,
  ariaLabel,
  findLabel,
  preset,
}: {
  options: FilterOption[];
  selected: ReadonlySet<string>;
  onChange: (next: ReadonlySet<string>) => void;
  disabled: boolean;
  /** The toggle's text and the clear button's, e.g. "All Sets". */
  allLabel: string;
  ariaLabel: string;
  findLabel?: string;
  preset?: { label: string; title: string; active: boolean; onSelect: () => void };
}) {
  const [open, setOpen] = useState(false);
  const [find, setFind] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelStyle = useDropdownPosition(open, toggleRef, panelRef, {
    align: "end",
    maxHeight: 340,
  });

  // Outside clicks close it, like any dropdown. The panel lives in a
  // portal, so "inside" means the toggle or the panel.
  useEffect(() => {
    if (!open) return;
    function onMouseDown(e: MouseEvent) {
      const target = e.target as Node;
      if (rootRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
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
    if (preset?.active) return preset.label;
    if (selected.size === 0) return allLabel;
    const labels = options.filter((o) => selected.has(o.value)).map((o) => o.label);
    return labels.length <= 3
      ? labels.join(", ")
      : `${labels.slice(0, 2).join(", ")} +${labels.length - 2}`;
  }, [options, selected, allLabel, preset]);

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
        ref={toggleRef}
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
      {open &&
        createPortal(
          <div ref={panelRef} className="multi-filter-panel" style={panelStyle}>
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
            <div className="multi-filter-presets">
              {preset && (
                <button
                  type="button"
                  className="btn-sm"
                  title={preset.title}
                  disabled={preset.active}
                  onClick={preset.onSelect}
                >
                  {preset.label}
                </button>
              )}
              <button
                type="button"
                className="btn-sm"
                disabled={selected.size === 0 && !preset?.active}
                onClick={() => onChange(new Set())}
              >
                {allLabel}
              </button>
            </div>
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
                  {o.icon}
                  <span className="mono">{o.label}</span>
                </label>
              ))}
              {shown.length === 0 && <p className="hint">No matches.</p>}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
