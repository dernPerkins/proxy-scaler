// A two-way choice drawn as one pill split down the middle, with a thumb
// that slides to the chosen half. For a setting with exactly two values
// where both read as equals (Scryfall vs MPC Fill art) — .segmented is the
// app's control for modes with more options, or toolbar-sized ones.
//
// A radiogroup: arrow keys move the choice, like a native radio pair. The
// thumb's slide is a CSS transform transition, dropped under
// prefers-reduced-motion (styles.css).
import type { KeyboardEvent } from "react";

export interface SplitPillOption<T extends string> {
  value: T;
  label: string;
  /** Tooltip; also the reason, when `disabled`, it can't be chosen. */
  title?: string;
  disabled?: boolean;
}

export default function SplitPill<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  disabled,
}: {
  options: readonly [SplitPillOption<T>, SplitPillOption<T>];
  value: T;
  onChange: (value: T) => void;
  ariaLabel: string;
  disabled?: boolean;
}) {
  const index = Math.max(
    0,
    options.findIndex((o) => o.value === value),
  );

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
    if (step === undefined) return;
    e.preventDefault();
    const next = options[(index + step + options.length) % options.length];
    if (!next.disabled && !disabled) onChange(next.value);
  }

  return (
    <div
      className={`split-pill${disabled ? " is-disabled" : ""}`}
      role="radiogroup"
      aria-label={ariaLabel}
      onKeyDown={onKeyDown}
    >
      <span
        className="split-pill-thumb"
        aria-hidden="true"
        style={{ transform: `translateX(${index * 100}%)` }}
      />
      {options.map((option, i) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={i === index}
          // Roving: only the chosen half is in the tab order.
          tabIndex={i === index ? 0 : -1}
          className={`split-pill-option${i === index ? " is-active" : ""}`}
          title={option.title}
          disabled={disabled || option.disabled}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
