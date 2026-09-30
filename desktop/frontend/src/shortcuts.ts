// App-wide keyboard shortcuts: which modifier counts, how to name it, and
// when a shortcut should stay quiet.
//
// The handlers themselves live next to the thing they drive — the tab
// switcher in App.tsx beside the tab list, New in ProjectBar.tsx beside
// the New button — so a shortcut can never drift from the click it
// stands in for.

// Cmd on macOS, Ctrl everywhere else: the platform's own "app command"
// key, matching what the browser and every other desktop app on that OS
// use for the same gestures. The other one is deliberately not accepted
// as an alias — Ctrl+N on a Mac is emacs-style next-line in text fields,
// and taking it away from an input would be a regression, not a bonus.
const IS_MAC =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad|iPod/.test(navigator.platform);

/** True when the event carries this platform's shortcut modifier and
 *  nothing else that would make it a different chord (Alt, or the other
 *  modifier). Shift is left alone so a caller can decide about it. */
export function hasShortcutModifier(e: KeyboardEvent): boolean {
  const primary = IS_MAC ? e.metaKey : e.ctrlKey;
  const other = IS_MAC ? e.ctrlKey : e.metaKey;
  return primary && !other && !e.altKey;
}

/** How to write the modifier in tooltips: "⌘" on a Mac, "Ctrl" elsewhere. */
export function shortcutLabel(key: string): string {
  return IS_MAC ? `⌘${key}` : `Ctrl+${key}`;
}

/** True while something is asking the user a question — a confirm, a
 *  progress modal, the image viewer, a tutorial step. Shortcuts stay out
 *  of it: switching tabs would unmount a page-level viewer, and New would
 *  double-trigger under its own "Discard this project?" confirm. Checked
 *  in the DOM rather than through state because every modal in the app
 *  portals through ModalOverlay (one class) and the tour draws in one
 *  root; there is no state that knows about all of them. */
export function isDialogOpen(): boolean {
  return document.querySelector(".modal-overlay, .tour-root") != null;
}
