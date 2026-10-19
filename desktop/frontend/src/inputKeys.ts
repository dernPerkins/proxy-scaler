import type { KeyboardEvent } from "react";

/** For a field that saves on blur: Enter saves too, by leaving the field
 *  (so there is one save path, and Enter can't save half an edit twice). */
export function blurOnEnter(e: KeyboardEvent<HTMLInputElement>): void {
  if (e.key !== "Enter") return;
  e.preventDefault();
  e.currentTarget.blur();
}
