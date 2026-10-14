// A single-choice dropdown drawn by the app rather than the OS.
//
// Why not <select>: the app styles every select (appearance: none, our
// chevron), but the list a select opens is still the platform's own menu,
// and on macOS that menu is positioned so the checked item sits over the
// control — a separate floating element on top of the box that was
// clicked, not a list hanging from it. This draws the list itself, under
// the toggle and the same width, like MultiSelectFilter's panel.
//
// Keyboard: Enter/Space/ArrowDown opens, arrows move, Enter picks, Esc
// closes, Tab leaves. Outside clicks close it.
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

export interface SelectMenuOption {
  value: string;
  label: string;
}

export default function SelectMenu({
  options,
  value,
  onChange,
  placeholder,
  disabled,
  ariaLabel,
}: {
  options: SelectMenuOption[];
  /** The chosen option's value, or null when none applies. */
  value: string | null;
  onChange: (value: string) => void;
  /** What the toggle shows when `value` matches no option, e.g. "Custom". */
  placeholder: string;
  disabled?: boolean;
  ariaLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const listId = useId();

  const selectedIndex = options.findIndex((o) => o.value === value);
  const current = selectedIndex >= 0 ? options[selectedIndex] : null;

  useEffect(() => {
    if (!open) return;
    function onMouseDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [open]);

  function openList() {
    if (disabled || options.length === 0) return;
    setActive(Math.max(selectedIndex, 0));
    setOpen(true);
  }

  function pick(index: number) {
    const option = options[index];
    setOpen(false);
    toggleRef.current?.focus();
    if (option && option.value !== value) onChange(option.value);
  }

  function onKeyDown(e: KeyboardEvent) {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        openList();
      }
      return;
    }
    switch (e.key) {
      case "Escape":
        // Close the list, not whatever dialog or page is listening for Esc.
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
        toggleRef.current?.focus();
        break;
      case "ArrowDown":
        e.preventDefault();
        setActive((i) => Math.min(i + 1, options.length - 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setActive((i) => Math.max(i - 1, 0));
        break;
      case "Home":
        e.preventDefault();
        setActive(0);
        break;
      case "End":
        e.preventDefault();
        setActive(options.length - 1);
        break;
      case "Enter":
      case " ":
        e.preventDefault();
        pick(active);
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  }

  return (
    <div className="select-menu" ref={rootRef} onKeyDown={onKeyDown}>
      <button
        ref={toggleRef}
        type="button"
        className="select-button select-menu-toggle"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        disabled={disabled}
        onClick={() => (open ? setOpen(false) : openList())}
      >
        <span className={current ? undefined : "select-menu-placeholder"}>
          {current ? current.label : placeholder}
        </span>
      </button>
      {open && (
        <ul
          id={listId}
          className="select-menu-list"
          role="listbox"
          aria-label={ariaLabel}
          aria-activedescendant={`${listId}-${active}`}
        >
          {options.map((o, i) => (
            <li
              key={o.value}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === selectedIndex}
              className={i === active ? "select-menu-option active" : "select-menu-option"}
              onMouseEnter={() => setActive(i)}
              // mousedown, not click: keeps focus on the toggle so the
              // outside-click listener never sees this as leaving.
              onMouseDown={(e) => {
                e.preventDefault();
                pick(i);
              }}
            >
              <span className="select-menu-check" aria-hidden="true">
                {i === selectedIndex ? "✓" : ""}
              </span>
              {o.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
