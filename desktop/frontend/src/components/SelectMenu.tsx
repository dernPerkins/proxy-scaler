// The app's dropdown: every single-choice picker uses this instead of a
// <select>.
//
// Why not <select>: the app styles every select box (appearance: none, our
// chevron), but the list a select opens is still the platform's own menu,
// and on macOS that menu is positioned so the checked item sits over the
// control — a separate floating element on top of the box that was
// clicked, not a list hanging from it. This draws the list itself.
//
// The list is portalled to document.body and placed by
// useDropdownPosition: under the toggle, or above it when there's no room
// below, and never clipped by a modal or scrolling panel. Being outside
// the toggle's DOM also matters for the <label className="field"> most
// pickers sit in — a click inside a label re-activates its control, so a
// list rendered inside one would reopen the moment an option was picked.
//
// Like a native select: the toggle is as wide as its longest option
// (inline pickers don't change width per pick; in a .field it fills the
// row and truncates), options can sit under group headers, and typing
// jumps to a matching option. Keyboard: Enter/Space/arrows open, arrows
// move, Home/End, Enter picks, Esc closes, Tab leaves.
import {
  Fragment,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { useDropdownPosition } from "../useDropdownPosition";

export interface SelectMenuOption {
  value: string;
  label: string;
  /** Options sharing a group are listed under a header with its name. */
  group?: string | null;
}

// How long typed characters keep building one search, as in a native
// select: "de" jumps to DE, a pause then "e" starts over at EN.
const TYPEAHEAD_RESET_MS = 500;

export default function SelectMenu({
  options,
  value,
  onChange,
  placeholder = "",
  disabled,
  ariaLabel,
  title,
}: {
  options: SelectMenuOption[];
  /** The chosen option's value, or null when none applies. */
  value: string | null;
  onChange: (value: string) => void;
  /** What the toggle shows when `value` matches no option, e.g. "Custom". */
  placeholder?: string;
  disabled?: boolean;
  /** For a picker with no visible label; one inside a <label> gets its
   *  name from the label. */
  ariaLabel?: string;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const typeahead = useRef({ text: "", at: 0 });
  // Set when Space picks an option: the toggle's own keyup activation
  // would otherwise reopen the list it just closed.
  const swallowClick = useRef(false);
  const listId = useId();
  const listStyle = useDropdownPosition(open, toggleRef, listRef);

  const selectedIndex = options.findIndex((o) => o.value === value);
  const current = selectedIndex >= 0 ? options[selectedIndex] : null;

  // Outside clicks close it. The list lives in a portal, so "inside"
  // means the toggle or the list.
  useEffect(() => {
    if (!open) return;
    function onMouseDown(e: MouseEvent) {
      const target = e.target as Node;
      if (rootRef.current?.contains(target) || listRef.current?.contains(target)) return;
      setOpen(false);
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [open]);

  // Keep the highlighted option in view: the selected one on open, then
  // wherever the arrows or typing take it.
  useLayoutEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${active}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [open, active, listStyle]);

  // A picker disabled while open (the server dropped) shouldn't leave its
  // list floating.
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  function openList(at = Math.max(selectedIndex, 0)) {
    if (disabled || options.length === 0) return;
    setActive(at);
    setOpen(true);
  }

  function close() {
    setOpen(false);
    toggleRef.current?.focus();
  }

  function pick(index: number) {
    const option = options[index];
    close();
    if (option && option.value !== value) onChange(option.value);
  }

  // The next option (after the highlighted one, wrapping) whose label
  // starts with what's been typed — a repeated single letter cycles.
  function typeaheadMatch(key: string): number {
    const now = Date.now();
    const t = typeahead.current;
    t.text = now - t.at > TYPEAHEAD_RESET_MS ? key : t.text + key;
    t.at = now;
    const query = t.text.toLowerCase();
    const repeated = query.length > 1 && [...query].every((c) => c === query[0]);
    const needle = repeated ? query[0] : query;
    const from = open ? active : Math.max(selectedIndex, 0);
    for (let step = repeated || query.length === 1 ? 1 : 0; step <= options.length; step++) {
      const i = (from + step) % options.length;
      if (options[i].label.toLowerCase().startsWith(needle)) return i;
    }
    return -1;
  }

  function onKeyDown(e: KeyboardEvent) {
    if (disabled) return;
    if (e.key.length === 1 && e.key !== " " && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const match = typeaheadMatch(e.key);
      if (match >= 0) {
        e.preventDefault();
        if (open) setActive(match);
        else openList(match);
      }
      return;
    }
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
        close();
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
        if (e.key === " ") swallowClick.current = true;
        pick(active);
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  }

  const list = open
    ? createPortal(
        <ul
          ref={listRef}
          id={listId}
          className="select-menu-list"
          role="listbox"
          aria-label={ariaLabel}
          style={listStyle}
        >
          {options.map((o, i) => {
            const header =
              o.group && o.group !== options[i - 1]?.group ? (
                <li className="select-menu-group" role="presentation">
                  {o.group}
                </li>
              ) : null;
            return (
              <Fragment key={o.value}>
                {header}
                <li
                  id={`${listId}-${i}`}
                  data-index={i}
                  role="option"
                  aria-selected={i === selectedIndex}
                  className={
                    "select-menu-option" +
                    (i === active ? " active" : "") +
                    (o.group ? " grouped" : "")
                  }
                  onMouseEnter={() => setActive(i)}
                  // Focus stays on the toggle; the pick itself waits for
                  // the click. Picking on mousedown would remove the list
                  // under the pointer, and the click that follows would
                  // land on whatever is beneath — a modal's backdrop, which
                  // closes the modal.
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => pick(i)}
                >
                  <span className="select-menu-check" aria-hidden="true">
                    {i === selectedIndex ? "✓" : ""}
                  </span>
                  {o.label}
                </li>
              </Fragment>
            );
          })}
        </ul>,
        document.body,
      )
    : null;

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
        aria-activedescendant={open ? `${listId}-${active}` : undefined}
        title={title}
        disabled={disabled}
        onClick={() => {
          if (swallowClick.current) {
            swallowClick.current = false;
            return;
          }
          if (open) setOpen(false);
          else openList();
        }}
        onKeyUp={() => {
          // Whatever Space did on keyup has happened by now.
          swallowClick.current = false;
        }}
      >
        {/* Every label stacked in one cell, only the current one visible:
            the toggle sizes to the longest, as a native select does. */}
        <span className="select-menu-value">
          {options.map((o) => (
            <span key={o.value} className="select-menu-sizer" aria-hidden="true">
              {o.label}
            </span>
          ))}
          {placeholder && (
            <span className="select-menu-sizer" aria-hidden="true">
              {placeholder}
            </span>
          )}
          <span className={current ? undefined : "select-menu-placeholder"}>
            {current ? current.label : placeholder}
          </span>
        </span>
      </button>
      {list}
    </div>
  );
}
