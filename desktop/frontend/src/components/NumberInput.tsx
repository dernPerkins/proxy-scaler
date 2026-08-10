import { useState } from "react";

// A controlled <input type="number"> that survives the intermediate text
// states typing produces.
//
// The problem it solves: for a number input the DOM reports `value` as ""
// whenever its contents aren't a *complete* valid number — "-", ".", "-."
// and "1e" all read as "". Feeding that straight into `Number(...)`
// yields 0, so a plain `onChange={e => set(Number(e.target.value))}`
// snaps the field to 0 the instant you type a leading "-", and React then
// re-renders "0" over the character you just typed. Negative values were
// unreachable by keyboard (only the spinner arrows could produce them),
// and the same bug ate the "." mid-way through typing a decimal.
//
// The fix is to let the raw text be the source of truth while editing and
// only commit parseable values upstream. Holding the draft as "" for the
// unparseable states matters: React only rewrites the DOM's value when it
// differs from the rendered prop, so rendering "" against a DOM value of
// "" is a no-op and the "-" the user typed stays on screen. Blur clears
// the draft so the field re-syncs to the canonical committed number.
interface NumberInputProps {
  /** Optional because several PdfLayoutRequest fields are declared
   *  optional; an absent value renders as an empty field. */
  value: number | undefined;
  onChange: (value: number) => void;
  step?: number;
  min?: number;
}

export default function NumberInput({ value, onChange, step, min }: NumberInputProps) {
  // null = not mid-edit, mirror `value`.
  const [draft, setDraft] = useState<string | null>(null);

  return (
    <input
      type="number"
      step={step}
      min={min}
      value={draft ?? (value == null ? "" : String(value))}
      onChange={(e) => {
        const raw = e.target.value;
        setDraft(raw);
        // "" covers both a genuinely empty field and the incomplete
        // states above — neither is a number worth committing. The
        // previous value stands until they finish typing.
        if (raw === "") return;
        const parsed = Number(raw);
        if (Number.isFinite(parsed)) onChange(parsed);
      }}
      onBlur={() => setDraft(null)}
    />
  );
}
