// Add Cards' Tokens Only switch (⌘T / Ctrl+T). Held here, outside the
// Decklist page, because switching tabs remounts that page: as page state
// the switch went quietly back off, and the next import resolved real
// cards. It lasts for the session and starts off again at launch — it
// says what's being searched right now, not a habit worth saving.
import { useSyncExternalStore, type Dispatch, type SetStateAction } from "react";

let tokensOnly = false;
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const setTokensOnly: Dispatch<SetStateAction<boolean>> = (next) => {
  const value = typeof next === "function" ? next(tokensOnly) : next;
  if (value === tokensOnly) return;
  tokensOnly = value;
  listeners.forEach((listener) => listener());
};

export function useTokensOnly(): [boolean, Dispatch<SetStateAction<boolean>>] {
  return [useSyncExternalStore(subscribe, () => tokensOnly), setTokensOnly];
}
