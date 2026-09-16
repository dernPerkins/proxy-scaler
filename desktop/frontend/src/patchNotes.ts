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
    version: "0.3.0",
    date: "September 2026",
    notes: [
      "Custom images — drop your own card art into the new Customs tab (or straight onto the Decklist) and it becomes a card in the project, with its own quantity and a slot in every PDF and ZIP export, mixed freely with Scryfall printings.",
      "Custom images print at the resolution you uploaded and are never upscaled, so the file you prepared is exactly what lands on the page — the Customs tab flags anything below about 300 DPI.",
      "Custom images reach a remote server on their own: the first Generate or export uploads whatever that server hasn't seen, with a progress dialog, and switching servers offers to do the same.",
      "One sort control shared by the Decklist, PDF, and Export tabs — the order you see is the order that prints and exports.",
      "Light-bordered cards no longer get a black band in the bleed from the thin dark row some Scryfall renders carry on their bottom edge, and freshly downloaded originals are cleaned of the dark fill under their rounded corners that could halo after upscaling.",
      "Smarter GPU memory use — the automatic tile size now steps through five levels based on the VRAM actually free, so cards with 10 GB or less no longer attempt the largest pass.",
      "Running out of VRAM mid-card now retries on the GPU at a smaller tile instead of dropping to the much slower CPU path.",
      "A model that did fall back to the CPU returns to the GPU once VRAM frees up, instead of every later card crawling on the CPU until the worker restarted.",
      "Windows: generation stays inside real VRAM instead of silently spilling into system RAM and crawling with the GPU pinned at 100%.",
      "Cutting-machine support — an Electronic cutter setting on the PDF tab prints Silhouette registration marks (3- or 4-point, Standard or Auto Sheet Feeder inset, marks oriented for how you load the sheet), with a callout of the matching Silhouette Studio settings.",
      "The page preview shows the marks and their keep-out zones, warns when cards sit under one, and page guides are kept out of the corners the cutter scans; marks can be hidden per side for duplex sheets.",
      "Download cut file (SVG) — every card's trim box plus the marks, in the cutter's orientation, ready to import into Silhouette Studio.",
    ],
  },
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
