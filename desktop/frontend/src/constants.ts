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
