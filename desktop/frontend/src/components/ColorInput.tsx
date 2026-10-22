// A color as "#RRGGBB": the native picker's swatch plus the hex beside it,
// both editing the same value. The text field holds a draft while it is
// being typed into and commits only a complete hex (with or without the
// "#"), so a half-typed value never reaches the setting; blurring drops
// whatever draft is left and shows the committed value again.
import { useState } from "react";

const HEX = /^#[0-9A-Fa-f]{6}$/;

export default function ColorInput({
  value,
  onChange,
  disabled,
  title,
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  /** Tooltip; also the reason, when `disabled`, it can't be changed. */
  title?: string;
  ariaLabel: string;
}) {
  const [draft, setDraft] = useState<string | null>(null);

  return (
    <div className={`color-input${disabled ? " is-disabled" : ""}`} title={title}>
      <input
        type="color"
        value={value}
        disabled={disabled}
        aria-label={`${ariaLabel} swatch`}
        onChange={(e) => onChange(e.target.value.toUpperCase())}
      />
      <input
        type="text"
        value={draft ?? value}
        disabled={disabled}
        spellCheck={false}
        aria-label={ariaLabel}
        onChange={(e) => {
          const typed = e.target.value.trim();
          setDraft(typed);
          const hex = typed.startsWith("#") ? typed : `#${typed}`;
          if (HEX.test(hex)) onChange(hex.toUpperCase());
        }}
        onBlur={() => setDraft(null)}
      />
    </div>
  );
}
