// The PDF and Export previews' note for Scryfall cards printing from their
// ~300 DPI original because they have no upscale (or none at the
// Preferred DPI) — they print, but softer than the rest of the sheet.
// A hint, not an error: nothing is left out.
const LISTED = 5;

export function OriginalsFallbackNote({ names }: { names: string[] | undefined }) {
  if (!names || names.length === 0) return null;
  const listed = names.length <= LISTED;
  return (
    <p className="hint" style={{ marginTop: 10 }} title={listed ? undefined : names.join("\n")}>
      <strong>
        {names.length} card(s) use their 300 DPI original
      </strong>{" "}
      — nothing generated for them yet. Generate them on the Decklist tab for a sharper
      print{listed ? `: ${names.join(", ")}.` : "."}
    </p>
  );
}
