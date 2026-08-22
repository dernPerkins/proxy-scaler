// The DPI values the app offers, shared by the Decklist tab (which picks
// what to *generate*) and the PDF tab (which picks which already-generated
// variant to *print*). Kept in one place so the two lists can't drift.
export const DPI_OPTIONS = [600, 800, 1200];

// Acronyms for compact spots (deck-list status badges, thumbnail labels)
// where the full model names wrap; raw enum values stay in API calls,
// filenames, and the Tasks table. Unknown models fall back to their raw
// id so a new backend model never renders blank.
export const MODEL_DISPLAY_NAMES: Record<string, string> = {
  realesrgan_anime_fast: "REAF",
  illustrationjanai: "IJ",
  ultrasharp_v2: "USV2",
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
