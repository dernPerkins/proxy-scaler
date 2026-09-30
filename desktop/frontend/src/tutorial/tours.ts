// The first-view walkthroughs: one base tour per screen, plus follow-up
// tours for pieces that only exist once the user has made something (card
// rows, a selected custom/back, a rendered PDF preview). A base tour
// covers only what is always on screen; each follow-up auto-shows the
// first time its piece actually appears (see useTourWhenPresent in
// tutorialStore.ts), so nobody gets walked past controls that aren't
// there yet.
//
// Targets are `data-tour="…"` attributes on the real elements. A step
// whose target isn't in the DOM when the tour starts is dropped (a
// Vulkan-only control, a cutter button on an older server); a step with
// no target shows as a centered card.

export type Placement = "top" | "bottom" | "left" | "right";

export interface TourStep {
  target?: string;
  title: string;
  body: string;
  placement?: Placement;
}

export interface Tour {
  steps: TourStep[];
  /** Follow-up tours replayed straight after this one from the ? button,
   *  whenever their piece is on screen at the time. */
  followUps?: TourId[];
}

export type TourId =
  | "connect"
  | "decklist"
  | "decklist-cards"
  | "customs"
  | "customs-card"
  | "backs"
  | "backs-back"
  | "pdf"
  | "pdf-preview"
  | "export"
  | "tasks";

export const TOURS: Record<TourId, Tour> = {
  connect: {
    steps: [
      {
        title: "Welcome to Proxy Scaler!",
        body: "Quick tour, promise. First choice: where the upscaling runs. For almost everyone the answer is this computer.",
      },
      {
        target: "gate-local",
        title: "Use this device: pick this one",
        body: "Everything runs on this computer, all in one. No server and no setup. If you're not sure, this is the right choice.",
        placement: "bottom",
      },
      {
        target: "gate-remote",
        title: "Connect to a server: optional",
        body: "Only for special setups, like running the upscaler on a gaming PC and using this app from a laptop. You don't need it to use the app, and you can switch later from the Decklist sidebar.",
        placement: "bottom",
      },
      {
        target: "tutorial-button",
        title: "Need a refresher?",
        body: "Every screen has one of these. Click it any time to replay that screen's tour.",
        placement: "left",
      },
    ],
  },

  decklist: {
    followUps: ["decklist-cards"],
    steps: [
      {
        target: "import-box",
        title: "This is the Decklist tab",
        body: "Let's take a quick look around, starting here: paste your decklist, one card per line. \"4 Lightning Bolt\" works, but we recommend including the set code and number, formatted like \"1 Sol Ring (c21) 263\". Choose a language, then click Import cards.",
        placement: "bottom",
      },
      {
        target: "custom-drop",
        title: "Or use your own art",
        body: "Drop image files anywhere on this page and each one becomes a card.",
        placement: "top",
      },
      {
        target: "tabs",
        title: "The tabs, left to right",
        body: "Roughly the workflow: build the deck, add your own art or backs, then turn it into a PDF or a ZIP. Tasks shows what's running. Ctrl+1 to Ctrl+6 (Cmd on a Mac) jump straight to a tab, and Ctrl+N starts a new project.",
        placement: "bottom",
      },
      {
        target: "project-bar",
        title: "Projects",
        body: "Each deck is a project. Give it a name to save it, and switch between projects here. You don't have to name it to get started.",
        placement: "bottom",
      },
      {
        target: "server-switcher",
        title: "Generation server",
        body: "Shows where upscaling runs. Local (this computer) is the default and all you need.",
        placement: "right",
      },
      {
        target: "model-select",
        title: "Upscale model",
        body: "We've pre-picked the recommended model for your GPU, so start with that. If results look off (odd textures, weird faces, crashes), try another one. The Vulkan models work on almost any graphics card. Generate a single card first and compare until you find one you like.",
        placement: "right",
      },
      {
        target: "dpi-targets",
        title: "Target DPI",
        body: "1200 is the default because every model already upscales 4×. Lower DPIs come from simply downscaling that result, and give you smaller files. Tick more than one to make several versions.",
        placement: "right",
      },
      {
        target: "vram-select",
        title: "GPU VRAM",
        body: "Match this to your graphics card's memory, or leave it on Auto if you have that option. If you get crashes or it slows to a crawl, go one tier lower.",
        placement: "right",
      },
      {
        target: "custom-upscale-select",
        title: "Custom images",
        body: "Your own uploaded images print as they are unless you change this. \"Upscale to target DPI\" brings a low-resolution upload up to the DPI you ticked above. \"Upscale 4×\" keeps the model's full result, up to 2400 DPI. Uploads that are already sharp enough are left alone either way.",
        placement: "right",
      },
      {
        target: "tutorial-button",
        title: "That's it for now!",
        body: "Once you import some cards, we'll show you around the card list. You can replay any tour with this button.",
        placement: "bottom",
      },
    ],
  },

  "decklist-cards": {
    steps: [
      {
        target: "deck-actions",
        title: "Your cards are in!",
        body: "\"Generate upscaled images\" queues every card at once. Download images just grabs the regular-resolution originals if you don't need upscaling. Sort changes the order here and in your PDF.",
        placement: "bottom",
      },
      {
        target: "card-row",
        title: "One row per card",
        body: "Each card has its own controls, so you can try things one card at a time.",
        placement: "bottom",
      },
      {
        target: "printing-picker",
        title: "Pick the printing",
        body: "Click the set to choose a different printing or art.",
        placement: "bottom",
      },
      {
        target: "card-qty",
        title: "Quantity",
        body: "How many copies of this card go into your PDF or ZIP.",
        placement: "bottom",
      },
      {
        target: "card-buttons",
        title: "Per-card actions",
        body: "Generate just this card, a quick way to test a model. Show opens the finished images (once there are some) so you can compare them with the original or regenerate. Remove takes the card out of the deck.",
        placement: "left",
      },
      {
        target: "card-variants",
        title: "Versions",
        body: "Every model and DPI you've generated for this card shows up here with its status. Keep what you like. The PDF tab picks which version to print.",
        placement: "bottom",
      },
    ],
  },

  customs: {
    followUps: ["customs-card"],
    steps: [
      {
        title: "Custom cards",
        body: "Your own art as card fronts: alters, tokens, playtest cards, anything. The library is shared across all your projects.",
      },
      {
        target: "custom-dropzone",
        title: "Add images",
        body: "Drop PNG, JPEG or WebP files here, or click to browse. Each image becomes a card named after its file.",
        placement: "right",
      },
      {
        target: "tutorial-button",
        title: "Next step: select a card",
        body: "Once you've added an image, click it to see its settings. We'll walk you through them then.",
        placement: "bottom",
      },
    ],
  },

  "customs-card": {
    steps: [
      {
        target: "custom-tile",
        title: "Your custom card",
        body: "The highlighted tile is the selected card, and its settings are in the sidebar. Click any other tile to switch.",
        placement: "right",
      },
      {
        target: "custom-add",
        title: "Add to project",
        body: "Puts this card into the current deck so it prints with everything else.",
        placement: "bottom",
      },
      {
        target: "custom-zoom",
        title: "Check the trim",
        body: "Opens the full image with the cut line drawn on it, so you can see exactly what ends up on the card.",
        placement: "right",
      },
      {
        target: "custom-settings",
        title: "Card settings",
        body: "Rename it, and tick \"already includes bleed\" if the file has a print border (MPC Fill downloads usually do). That way it isn't padded twice.",
        placement: "right",
      },
    ],
  },

  backs: {
    followUps: ["backs-back"],
    steps: [
      {
        title: "Card backs",
        body: "Art for the back of your cards, if you're printing double-sided. Totally optional: skip it for front-only prints.",
      },
      {
        target: "back-dropzone",
        title: "Add a back",
        body: "Drop an image here or click to browse. Your backs are shared across every project.",
        placement: "right",
      },
      {
        target: "tutorial-button",
        title: "Next step: pick a back",
        body: "Click a back to use it for this project, and we'll show you its settings. Turn on back printing from the PDF tab.",
        placement: "bottom",
      },
    ],
  },

  "backs-back": {
    steps: [
      {
        target: "back-tile",
        title: "This project's back",
        body: "The highlighted back is the one this project prints with. Click another to switch.",
        placement: "right",
      },
      {
        target: "back-zoom",
        title: "Check the trim",
        body: "Opens the full image with the cut line drawn on it, so you can see exactly what ends up on the back. Looking doesn't switch this project's back.",
        placement: "right",
      },
      {
        target: "back-bleed",
        title: "Already has bleed?",
        body: "Tick this if the image already has a print border (MakePlayingCards backs do). That way it's trimmed to fit instead of padded twice.",
        placement: "right",
      },
      {
        target: "back-default",
        title: "Default for new projects",
        body: "Makes new projects start with this back. Existing projects keep their own.",
        placement: "right",
      },
    ],
  },

  pdf: {
    followUps: ["pdf-preview"],
    steps: [
      {
        target: "pdf-source",
        title: "Source images",
        body: "Chooses which of your generated versions go into the PDF. This never generates anything new, it only picks among what you already have.",
        placement: "right",
      },
      {
        target: "pdf-layout",
        title: "Layout",
        body: "Paper size, cards per page, spacing and bleed. The default 3×3 on A4 or 4×2 on Letter is what most people want.",
        placement: "right",
      },
      {
        target: "pdf-decklist",
        title: "Card order",
        body: "Sets the order cards appear across the pages.",
        placement: "right",
      },
      {
        target: "pdf-guides",
        title: "Cutting guides",
        body: "Card Guides are the corner cutting marks. Page Guides are the lines leading from the edge of the grid to the edge of the paper. Both are very helpful for lining up on manual cutters. Important to note: the lines sit just outside the edge of the cards, so no guide print ever lands on a card.",
        placement: "left",
      },
      {
        target: "pdf-backs",
        title: "Back printing",
        body: "Adds a back page behind every sheet for double-sided printing, using the back from the Backs tab.",
        placement: "left",
      },
      {
        target: "pdf-cutter",
        title: "Electronic cutter",
        body: "Only if you own a cutting machine like a Silhouette: this adds the registration marks it scans. Otherwise leave it off.",
        placement: "left",
      },
      {
        target: "pdf-download",
        title: "Make the PDF",
        body: "Builds and saves the PDF. Once your cards are generated, a preview of page 1 appears above, and we'll point out what to check there.",
        placement: "top",
      },
    ],
  },

  "pdf-preview": {
    steps: [
      {
        target: "pdf-summary",
        title: "Your print run",
        body: "How many cards and sheets you're printing. Anything missing a generated image is listed here, so nothing drops out without you knowing.",
        placement: "bottom",
      },
      {
        target: "pdf-preview-side",
        title: "Front and back",
        body: "With back printing on, flip between the two sides to make sure the backs line up.",
        placement: "bottom",
      },
      {
        target: "pdf-page-preview",
        title: "Page preview",
        body: "Exactly what page 1 will look like. Tweak the layout on the left and it updates, so check it here before you use any paper.",
        placement: "top",
      },
      {
        target: "pdf-cut-file",
        title: "Cut file",
        body: "An SVG outline of every card for cutting machines. You can ignore it if you cut by hand.",
        placement: "top",
      },
    ],
  },

  export: {
    steps: [
      {
        target: "export-source",
        title: "Source images",
        body: "Chooses which of your generated versions to export. These are the same settings the PDF tab uses.",
        placement: "right",
      },
      {
        target: "export-output",
        title: "Output",
        body: "Image format and bleed. JPG keeps files small for uploading to vendors. Most print shops want bleed included.",
        placement: "right",
      },
      {
        target: "export-buttons",
        title: "Export",
        body: "A plain ZIP of your images, or a ready-to-upload ZIP for TCGPlaytest (that one needs a back chosen on the Backs tab).",
        placement: "top",
      },
    ],
  },

  tasks: {
    steps: [
      {
        title: "Tasks",
        body: "Every download and upscale waits in a queue here. Useful when you want to see what's happening.",
      },
      {
        target: "tasks-summary",
        title: "Queue at a glance",
        body: "Shows whether the worker is running and how many tasks are pending, running, done or failed. Cancel All clears whatever hasn't started. Retry All re-runs failures.",
        placement: "bottom",
      },
      {
        target: "tasks-table",
        title: "Each task",
        body: "One row per image being made. If something failed, the error is shown here. Retrying it, or trying a different model, usually fixes it.",
        placement: "top",
      },
    ],
  },
};

/** Which base tour the ? button replays on each route. */
export const ROUTE_TOURS: Record<string, TourId> = {
  "/decklist": "decklist",
  "/customs": "customs",
  "/backs": "backs",
  "/pdf": "pdf",
  "/export": "export",
  "/tasks": "tasks",
};
