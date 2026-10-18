// What's printed on a card when that isn't the card's own name: a
// Universes Beyond printing's flavor name ("Cloud Strife" on Najeela, the
// Blade-Blossom), else a non-English printing's printed name (対抗呪文 on
// a Japanese Counterspell). The card's own name always leads — it is what
// was imported, sorts, searches and generates — and this rides beside it.

/** The name printed on the card, or null when it is just `name`. With
 *  `face`, one face of a double-faced card ("A // B" names split). */
export function alternateName(
  name: string,
  printedName: string | null | undefined,
  flavorName: string | null | undefined,
  face?: number | null,
): string | null {
  let alt = (flavorName || printedName || "").trim();
  let own = name;
  if (face != null) {
    alt = alt.split(" // ")[face] ?? alt;
    own = name.split(" // ")[face] ?? name;
  }
  return alt && alt !== own.trim() ? alt : null;
}
