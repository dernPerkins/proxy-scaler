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
    version: "0.5.0",
    date: "October 2026",
    notes: [
      "MPC Fill art - print any card with community art from MPC Fill. The printing picker has a Scryfall | MPC Fill switch: search by name, narrow it down by card type, language, minimum DPI and uploader, and give each face of a double-faced card its own art. Previews are Google's copies; the full file downloads only once you use it.",
      "Add Cards - the Decklist's import box is now a search. Type part of a name and matching cards list as you go (\"so rin\" finds Sol Ring, typos too); Enter adds the highlighted card, Ctrl+Enter (Cmd on a Mac) opens Card Search on it to pick a printing or MPC Fill art first. Switch to List to paste a whole decklist. Ctrl+K opens a command palette that searches the same way and holds the other ways to add: a pasted list, your own art, an MPC Fill order.",
      "Art From - next to Add Cards, choose Scryfall or MPC Fill. MPC Fill puts its latest community art on every face of each imported card, and any of them can be changed from the card's printing button. Cards with no MPC Fill match keep their Scryfall art.",
      "MPC Fill Catalog - MPC Fill searches run against a catalog you download once, from the launch prompt or the sidebar, and update when a newer one is out. At the request of MPC Fill's maintainer the app never contacts MPC Fill itself; art comes from Google Drive through your own generation server.",
      "MPC Fill tab - art you've downloaded from MPC Fill gets its own library, apart from your uploads, with search and filters, shown 30, 60 or 120 a page. Search all of MPC Fill from there, and Add to Project puts the art on its card in the current deck. MPC Fill art is always treated as carrying 3.175 mm (1/8\") of bleed. Delete All MPC Fill Downloads in its sidebar clears the library; cards keep the art they chose and download it again when needed.",
      "MPC Autofill orders - import the .xml from MPC Fill's Download XML button on the Decklist tab. Its fronts and backs are fetched into your MPC Fill and Back libraries and the order's cards are added with their quantities and backs; you're offered the order's card back as the project's.",
      "Printing picker - click a card's set on the Decklist to see every printing as images, newest first, showing what Scryfall would list for your project's language. Filter by set and language, sort, include Arena-only printings with Show Digital, and check each one full size alongside every version you've already generated. A double-faced card's front and back are chosen separately, each from any printing or MPC Fill art; the back follows the front's printing until you give it its own.",
      "Go through your whole deck in the picker - Prev and Next (Alt+← and Alt+→) move through the deck in its Decklist order, so you can choose art for every card in one sitting. Nothing downloads until you Confirm, which then downloads every card you looked at, both faces of a double-faced card included; closing any other way asks whether to keep your choices.",
      "Per-card backs - the little card on a row gives that card its own back, leaves it blank, or uploads a new back for it; everything else keeps the universal back from the Backs tab. The PDF and both ZIP exports honour it.",
      "Presets for the PDF and ZIP tabs - save your usual layout, offsets, guides and back printing (PDF) or format and bleed (ZIP) and pick them next time. Your first preset becomes the starting point for new projects, and Default puts the built-in settings back.",
      "Guides you can style - the PDF tab's Guides section sets Card Guides (at each card's trim corners) and Page Guides (from the page edge in to the grid) separately: color, solid or dashed, square or rounded corners, width, length, and whether they print on fronts and backs.",
      "Radius - rounded Card Guides and the cut file follow the modern 2.5 mm card corner. Radius in the Guides section matches other dies: Proper Alpha Cards gives Limited Edition Alpha printings Alpha's rounder 4 mm corner, All Alpha Cards gives it to every card, and Custom takes any radius you type in. With Proper Alpha Cards, a deck that holds Alpha cards gets one cut file per sheet, in a ZIP from the SVG Export.",
      "New projects start with the Language, All Languages and Art From you last chose. Projects you already have keep their own.",
      "Adding a card that's already in the deck adds its copies to that card instead of making a second row. A card given MPC Fill art from the MPC Fill tab stays a card of its own.",
      "Cut file (SVG) no longer carries the registration marks - Silhouette Studio cuts every shape in an imported SVG, so the marks were being cut on every sheet. Turn registration marks on in Studio's Page Setup instead and match them to the PDF tab's settings.",
      "Cards print without an upscale - a Scryfall card with nothing generated at your DPI now prints from its ~300 DPI original instead of being left out. The PDF and ZIP previews say which cards, since they'll come out softer.",
      "Set symbols on every card and in the picker, colored by rarity. Universes Beyond printings show the name printed on them (Cloud Strife on Najeela, the Blade-Blossom) alongside the card's own.",
      "View on a card's Original opens it full size.",
      "The tab bar and toolbar stay put while you scroll, the sidebar scrolls on its own, and a Back to Top button appears once you scroll down. The Customs and Backs tabs have a refreshed upload and search area.",
      "The Tasks tab shows each task's type: Download, Upscale or Add (your own art or backs added as they are). Tasks queued by an earlier version show a dash.",
    ],
  },
  {
    version: "0.4.0",
    date: "September 2026",
    notes: [
      "Custom images can be upscaled - a new Custom images setting on the Decklist tab: Upscale to target DPI upscales an upload to each ticked DPI it doesn't already reach, and Upscale 4× (up to 2400 DPI) keeps the model's full result. Only uploads below a ticked target are upscaled; one too large for your GPU is skipped with a message on its task and prints as uploaded.",
      "Upscale from the Customs and Backs tabs - each tab now has its own upscale settings and an Upscale button for the selected image, without it being in any project. Backs can be upscaled for the first time. Results are shared: every project that prints the image uses them, and badges under the button list every version that exists.",
      "Full-image viewer on the Customs and Backs tabs - the magnifier on a tile opens the upload with the trim line drawn on it, rounded to the real card corners, with anything cropped off to fit the card shaded. The name and bleed settings are editable right there.",
      "Vulkan models - a second group in the model dropdown that runs on almost any graphics card in every build. They exist for GPUs the regular path can't use reliably, such as AMD cards where ROCm or DirectML returned black or garbled images. Expect some to be slower than the regular entries; use them when the regular ones fail on your card.",
      "GPU VRAM dropdown replaces the tile-size number box - Low (4 GB or less), Medium (6-8 GB), High (12 GB+), Max (16 GB+), with Auto measuring free VRAM per card for the regular models. A pass that fails at one tier retries at the next lower one, then on the CPU.",
      "Compare any two versions - the caret next to Compare on a card's images lets you pick what the slider compares against: the original or any other DPI and model, so you can put two models side by side.",
      "Guided tutorials - each screen walks you through its controls the first time you open it, with follow-ups the first time cards land, an image is selected, or a preview renders. Replay any of them from the ? button at the end of the tab bar. All skippable if you're feeling familiar enough at this point.",
      "Keyboard shortcuts - Ctrl+1 to Ctrl+6 (Cmd on a Mac) jump straight to a tab, and Ctrl+N starts a new project.",
      "Generated images on the Decklist show up faster and use far less memory - the tiles load small previews instead of decoding a ~50 MB bitmap per 1200 DPI card.",
      "Card lookups no longer pick an Art Series card, token, emblem or other non-playable printing over the real card of the same name. Would happen on rare occasions, nuisance.",
      "Windows: saving a PDF, ZIP, cut file or image with an edited name no longer drops the extension.",
      "Windows: the Directories folder buttons failed with \"file not found\" in remote mode, and tried to SSH into your own machine when the server app ran locally. Both now just open the folder.",
      "AMD on Windows (DirectML): tiles no longer come back black or as noise; running out of GPU memory now retries at a smaller tile or on the CPU instead of failing the task, no longer leaves every later card on the CPU, and no longer holds the GPU memory until restart.",
      "Danger Zone -> Delete all now keeps your uploaded custom images.",
    ],
  },
  {
    version: "0.3.3",
    date: "September 2026",
    notes: [
      "Bleed now fans out of the rounded corners - every bleed pixel takes the color of the nearest point on the card's edge, so corners spread radially instead of filling a square block.",
      "Gold-bordered cards (Pro Tour Collector Set and the World Championship decks) print cleanly - the white arc on every corner, the white bars stretched out of the corners into the bleed, and the white streak across the top and bottom bleed are gone.",
      "Even beyond that, every edge defect Scryfall's renders carry - the phantom dark row on some white cards, a phantom white column on a Secret Lair print, the gold template's rim are handled by one rule instead of one fix each, so odd edges no longer need a color signature the code has to recognize.",
      "Custom images that already include bleed (MakePlayingCards downloads, for example) - tick \"This image already includes bleed\" in the new Customs sidebar.",
      "Images you've already generated unfortunately are not going to be retroactively fixed for artifacts. Ideally, you use the \"Danger Zone\" -> \"Delete All\" button and restart. I promise the results will be worth it going forward if you are reprinting often.",
    ],
  },
  {
    version: "0.3.2",
    date: "September 2026",
    notes: [
      "Fixed every generation task failing with \"operator torchvision::nms does not exist\" - the 0.3.0 and 0.3.1 installers shipped without a torchvision component the upscaler needs, after torchvision 0.29 renamed the file that the packaging step bundles. All Linux builds (CUDA, CUDA legacy, ROCm) were affected, along with some macOS and Windows builds; the build now fails outright if that component is missing, so it can't ship silently again.",
      "After updating, use Retry All in the Tasks panel to re-run any cards that failed with that error.",
    ],
  },
  {
    version: "0.3.1",
    date: "September 2026",
    notes: [
      "Export with bleed - a new option on the Export tab adds a bleed border to every image in the ZIP, defaulting to 3 mm. The bleed size is adjustable, and a selected back marked as already including bleed is handled correctly.",
      "Export as PNG or JPG - JPG is now the default, re-encoding every image for much smaller vendor uploads (TCGPlaytest, MakePlayingCards) with no visible difference in print; PNG ships the stored images exactly as they are.",
      "ZIP exports that re-render (bleed or JPG) now show the same per-image progress dialog as the PDF, with a Cancel button.",
      "Lowercase card names resolve - typing a name like \"verdant kraken\" without a local card database no longer fails with \"Card not found\". Scryfall's fuzzy search isn't reliably case-insensitive, so names are now matched exactly first, with fuzzy matching kept as the fallback for typos and partial names. Using a database avoids API calls and is still highly recommend to download.",
    ],
  },
  {
    version: "0.3.0",
    date: "September 2026",
    notes: [
      "Custom images - drop your own card art into the new Customs tab (or straight onto the Decklist) and it becomes a card in the project, with its own quantity and a slot in every PDF and ZIP export, mixed freely with Scryfall printings.",
      "Custom images print at the resolution you uploaded and are never upscaled, so the file you prepared is exactly what lands on the page - the Customs tab flags anything below about 300 DPI.",
      "Custom images reach a remote server on their own: the first Generate or export uploads whatever that server hasn't seen, with a progress dialog, and switching servers offers to do the same.",
      "One sort control shared by the Decklist, PDF, and Export tabs - the order you see is the order that prints and exports. On the PDF tab it now lives under a Deck list heading with the other settings.",
      "Light-bordered cards no longer get a black band in the bleed from the thin dark row some Scryfall renders carry on their bottom edge, and freshly downloaded originals are cleaned of the dark fill under their rounded corners that could halo after upscaling.",
      "Smarter GPU memory use - the automatic tile size now steps through five levels based on the VRAM actually free, so cards with 10 GB or less no longer attempt the largest pass.",
      "Running out of VRAM mid-card now retries on the GPU at a smaller tile instead of dropping to the much slower CPU path.",
      "A model that did fall back to the CPU returns to the GPU once VRAM frees up, instead of every later card crawling on the CPU until the worker restarted.",
      "Windows: generation stays inside real VRAM instead of silently spilling into system RAM and crawling with the GPU pinned at 100%.",
      "Cutting-machine support - an Electronic cutter setting on the PDF tab prints Silhouette registration marks (3- or 4-point, Standard or Auto Sheet Feeder inset, marks oriented for how you load the sheet).",
      "The page preview shows the marks and their keep-out zones, warns when cards sit under one, and page guides are kept out of the corners the cutter scans; marks can be hidden per side for duplex sheets.",
      "Download cut file (SVG) - every card's trim box plus the marks, in the cutter's orientation, ready to import into Silhouette Studio. It also works with no cutter selected, as the trim boxes alone: the route for a Cricut or any machine that cuts against the mat instead of scanning marks.",
    ],
  },
  {
    version: "0.2.1",
    date: "August 2026",
    notes: [
      "Faster multi-DPI runs - generating a card at several DPIs now shares one inference pass instead of regenerating per DPI.",
      "New UltrasharpV2 Lite model - near-V2 quality in a fraction of the time.",
      "CPU fallback notice - a dialog now tells you when Multistage generation falls back to the CPU, instead of it just running slowly.",
      "Clearer TCGPlaytest export messaging - the reason the button is disabled now sits directly under it.",
      "This Patch Notes dialog - reopen it anytime by clicking the version number at the end of the tab bar.",
    ],
  },
  {
    version: "0.2.0",
    date: "August 2026",
    notes: [
      "Card back support - upload back images, pick one per project, and print paired fronts and backs.",
      "New Export tab - download your images as a ZIP, including the TCGPlaytest paired front/back format.",
      "Option to export the 300 DPI Scryfall originals without generating.",
      "Resume prompt now also appears for tasks left over on remote servers.",
    ],
  },
];
