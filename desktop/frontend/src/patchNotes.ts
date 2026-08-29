// The client's bundled release history, shown by PatchNotesPrompt.tsx —
// auto-opened once per release, and reopenable anytime from the version
// number in the tab bar. Bundled rather than fetched on purpose: the
// update manifest's `notes` string describes the NEXT release (the one
// being offered), never the one that's running, and a dialog about "what
// you just got" must work offline on first launch after an install.
//
// Like config.ts's *_MIN_SERVER_VERSION constants, the version strings
// here name historical releases forever — this file must NEVER be added
// to packaging/set-version.py's FILES list. set-version.py's check()
// instead verifies the newest entry matches the release being cut, so a
// release can't ship without its own notes (see docs/releasing.md).
//
// Newest first. Plain-string bullets, rendered as list items — no
// markdown on purpose (no dep, nothing for the CSP to worry about).

export interface PatchNotesEntry {
  version: string;
  /** Human-readable release date, e.g. "August 2026". */
  date: string;
  notes: string[];
}

export const PATCH_NOTES: PatchNotesEntry[] = [
  {
    version: "0.2.1",
    date: "August 2026",
    notes: [
      "Faster multi-DPI runs — generating a card at several DPIs now shares one inference pass instead of regenerating per DPI.",
      "New UltrasharpV2 Lite model — near-V2 quality in a fraction of the time.",
      "CPU fallback notice — a dialog now tells you when Multistage generation falls back to the CPU, instead of it just running slowly.",
      "Clearer TCGPlaytest export messaging — the reason the button is disabled now sits directly under it.",
      "This Patch Notes dialog — reopen it anytime by clicking the version number at the end of the tab bar.",
    ],
  },
  {
    version: "0.2.0",
    date: "August 2026",
    notes: [
      "Card back support — upload back images, pick one per project, and print paired fronts and backs.",
      "New Export tab — download your images as a ZIP, including the TCGPlaytest paired front/back format.",
      "Option to export the 300 DPI Scryfall originals without generating.",
      "Resume prompt now also appears for tasks left over on remote servers.",
    ],
  },
];
