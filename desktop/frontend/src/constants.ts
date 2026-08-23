// The DPI values the app offers, shared by the Decklist tab (which picks
// what to *generate*) and the PDF tab (which picks which already-generated
// variant to *print*). Kept in one place so the two lists can't drift.
export const DPI_OPTIONS = [600, 800, 1200];

// Export DPI is a print-density choice, not a variant selector, so it
// carries 300 too — printing straight from the ~300 DPI Scryfall
// originals is a supported path (see ORIGINAL_MODEL below), and any
// source gets resized to this density at render time regardless.
export const EXPORT_DPI_OPTIONS = [300, 600, 800, 1200];

// Sentinel variant for a download-only Scryfall original (no upscale) —
// mirrors ORIGINAL_MODEL/ORIGINAL_DPI in proxy_scaler/dpi.py. Never a
// pickable upscale model (the /api/models list can't contain it).
export const ORIGINAL_MODEL = "original";
export const ORIGINAL_DPI = 300;

// Acronyms for compact spots (deck-list status badges, thumbnail labels)
// where the full model names wrap; raw enum values stay in API calls,
// filenames, and the Tasks table. Unknown models fall back to their raw
// id so a new backend model never renders blank.
export const MODEL_DISPLAY_NAMES: Record<string, string> = {
  realesrgan_anime_fast: "REAF",
  illustrationjanai: "IJ",
  ultrasharp_v2: "USV2",
  [ORIGINAL_MODEL]: "Original",
};

export function modelDisplayName(model: string): string {
  return MODEL_DISPLAY_NAMES[model] ?? model;
}

// The generation-directory names every client request carries. Relative
// on purpose — the server resolves them against its own cwd, and a path
// valid on one machine means nothing against a Remote host (see
// ARCHITECTURE.md). misc.py's /api/paths mirrors them and reports where
// they actually land.
//
// Lives here rather than in DecklistPage.tsx now that the Backs tab needs
// weights_dir too, for the same reason DPI_OPTIONS is here: two copies of
// the same list are two lists that eventually disagree.
export const DEFAULT_GEN_PATHS = {
  output_dir: "output",
  cache_dir: "imgcache",
  weights_dir: "weights",
};
