// The DPI values the app offers, shared by the Decklist tab (which picks
// what to *generate*) and the PDF tab (which picks which already-generated
// variant to *print*). Kept in one place so the two lists can't drift.
export const DPI_OPTIONS = [600, 800, 1200];

// Short human-readable model names for compact spots (deck-list status
// badges, thumbnail labels). These mirror the display half of
// UpscaleModel.label in proxy_scaler/upscale.py; raw enum values stay in
// API calls, filenames, and the Tasks table. Unknown models fall back to
// their raw id so a new backend model never renders blank.
export const MODEL_DISPLAY_NAMES: Record<string, string> = {
  realesrgan_anime_fast: "Real-ESRGAN Anime Fast",
  illustrationjanai: "IllustrationJaNai",
  ultrasharp_v2: "UltraSharpV2",
};

export function modelDisplayName(model: string): string {
  return MODEL_DISPLAY_NAMES[model] ?? model;
}
