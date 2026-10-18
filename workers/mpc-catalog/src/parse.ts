/**
 * Turns Drive folder and file names into catalogue records, following
 * .scratch/mpcfill-art-picker/phase-c/format.md.
 *
 * These rules were written from folder and file names observed in public
 * MPC Fill source folders, not from mpc-autofill or CriProx (both GPL).
 * Shapes seen so far:
 *
 *   folders  `!Misc`, `Borderless ` (trailing space), `Cardbacks`, `2025`,
 *            `!Mursu [Retro, Gold Border, Ancient]`, `!Removed`,
 *            `Archenemy (Complete) [Scryfall Scan]`, `{FR}  Tokens`,
 *            `{DE} LotR [Universes Beyond]`, `PT - CARD`, `PT_PW`,
 *            `Rethemed to LotR [Alternate Art,Extended,Nickname,Universes Beyond]`
 *   files    `Regal Behemoth (CMM, Borderless).png`,
 *            `Alien (Token) [WHO] {2}.jpg` (set code + collector number),
 *            `Angel (Token) [RVR] {3} (1).jpg` (Drive's duplicate suffix),
 *            `Ajani Steadfast (Emblem).jpg`, `Assassin (Token).png`,
 *            `Ezio Auditore da Firenze (Borderless, Luisa J.png` (Drive
 *            truncated it mid-group), `Forest - 301.png`, `Strip Mine - 82b.png`,
 *            `Sol Ring - Copy (2).png`, `Mishra_s Factory.png` (left as is)
 *   noise    MTG Arena texture dumps (`418951_AIF`, `Default-Particle #229402`)
 *            and set-symbol PNGs: squares, 2:1 and other non-card shapes
 */

/** MPC Fill's language codes. */
export const LANGUAGES = ["EN", "ES", "FR", "DE", "IT", "PT", "JA", "RU", "ZH", "AR", "SA"] as const;
export type Language = (typeof LANGUAGES)[number];
const LANGUAGE_CODES = new Set<string>(LANGUAGES);
export const DEFAULT_LANGUAGE: Language = "EN";

// Whole folder names (after tags are removed) that name a language.
const LANGUAGE_NAMES: Record<string, Language> = {
  english: "EN",
  spanish: "ES",
  español: "ES",
  espanol: "ES",
  french: "FR",
  français: "FR",
  francais: "FR",
  german: "DE",
  deutsch: "DE",
  italian: "IT",
  italiano: "IT",
  portuguese: "PT",
  português: "PT",
  portugues: "PT",
  japanese: "JA",
  日本語: "JA",
  russian: "RU",
  русский: "RU",
  chinese: "ZH",
  中文: "ZH",
  arabic: "AR",
};

export type CardType = "CARD" | "TOKEN" | "CARDBACK";

/** One line of a shard. Field meanings are in format.md. */
export interface CatalogRecord {
  id: string;
  name: string;
  ext: "png" | "jpg" | "jpeg";
  type: CardType;
  lang: string;
  tags: string[];
  dpi: number;
  w: number;
  h: number;
  size: number;
  created: string;
  modified: string;
  folder: string;
}

/** The fields requested from Drive's files.list. */
export interface DriveFile {
  id: string;
  name: string;
  mimeType: string;
  size?: string;
  createdTime?: string;
  modifiedTime?: string;
  imageMediaMetadata?: { width?: number; height?: number };
}

export const FOLDER_MIME = "application/vnd.google-apps.folder";

// MPC's card is 63 x 88 mm; with its 3.175 mm (1/8") bleed on each side the
// long edge is 94.35 mm. Matches dpi.dpi_at_card_size on the server.
const BLED_LONG_EDGE_IN = (88 + 2 * 3.175) / 25.4;

const SQUARE_GROUP = /\[([^\]]*)\]/g;
const ANY_GROUP = /\[[^\]]*\]|\([^)]*\)|\{[^}]*\}/g;
// `[WHO] {2}`: a set code followed by a collector number. Not tags.
const SET_AND_NUMBER = /\[\s*[A-Za-z0-9]{2,6}\s*\]\s*\{[^}]*\}/g;
const BRACE_GROUP = /\{\s*([^}]*?)\s*\}/g;
const IMAGE_EXT = /\.(png|jpe?g)$/i;
// A discard pile: the folder and everything below it are skipped. "Old"
// counts only alone or as "Old Version(s)/Stuff/Cards": "Old School" is
// real old-border art.
export const DISCARD_FOLDER =
  /^(?:(?:removed|trash|deprecated|unused)\b|old(?:\s+(?:versions?|stuff|cards))?\s*(?:[[({].*)?$)/i;
// `PT - CARD`, `PT CARDS`, `PT_PW`: an upper-case code leading the name.
const LEADING_CODE = /^([A-Z]{2})(?:[\s_-]|$)/;
// A group Drive's name truncation left open: `… (Borderless, Luisa J`.
const UNCLOSED_GROUP = /[([{][^)\]}]*$/;
// ` - Copy` (its `(2)` is already gone with the other groups) or a
// collector number ` - 301`, ` - 82b`, stripped repeatedly.
const TRAILING_SUFFIX = /\s+-\s+(?:copy|\d+[a-z]?)$/i;

// Card-shaped width/height ratios: portrait cards (63:88 is 0.716) and
// landscape planes and battles. Anything else (squares, 2:1 textures) and
// anything below MIN_DPI isn't a card image.
const PORTRAIT_RATIO: readonly [number, number] = [0.62, 0.8];
const LANDSCAPE_RATIO: readonly [number, number] = [1.25, 1.6];
export const MIN_DPI = 200;

/** A leading `!` only sorts a folder to the top; it isn't part of the name. */
export function stripSortPrefix(name: string): string {
  return name.replace(/^\s*!+\s*/, "");
}

export function isDiscardFolder(name: string): boolean {
  return DISCARD_FOLDER.test(stripSortPrefix(name).trim());
}

/** CARDBACK wins over TOKEN, which wins over CARD. */
export function cardType(folders: readonly string[]): CardType {
  if (folders.some((f) => /card ?backs?/i.test(f))) return "CARDBACK";
  if (folders.some((f) => /tokens?/i.test(f))) return "TOKEN";
  return "CARD";
}

export function dpiFor(w: number, h: number): number {
  const longEdge = Math.max(w, h);
  return longEdge > 0 ? Math.round(longEdge / BLED_LONG_EDGE_IN) : 0;
}

/**
 * Whether an image of w x h pixels can be a card: a portrait or landscape
 * card shape at MIN_DPI or more. Unknown dimensions (0) are given the
 * benefit of the doubt.
 */
export function isCardShaped(w: number, h: number): boolean {
  if (w <= 0 || h <= 0) return true;
  const ratio = w / h;
  const inRange = ([lo, hi]: readonly [number, number]) => ratio >= lo && ratio <= hi;
  return (inRange(PORTRAIT_RATIO) || inRange(LANDSCAPE_RATIO)) && dpiFor(w, h) >= MIN_DPI;
}

function normaliseTag(tag: string): string {
  return tag.toUpperCase() === "NSFW" ? "NSFW" : tag;
}

/** Bracket-group tags in order, comma-split and trimmed (not deduped). */
export function bracketTags(name: string): string[] {
  const tags: string[] = [];
  for (const match of name.replace(SET_AND_NUMBER, " ").matchAll(SQUARE_GROUP)) {
    for (const part of match[1].split(",")) {
      const tag = part.trim();
      if (tag) tags.push(normaliseTag(tag));
    }
  }
  return tags;
}

/** Concatenates tag lists, keeping the first spelling of each (case-insensitively). */
export function mergeTags(...lists: readonly string[][]): string[] {
  const seen = new Set<string>();
  const merged: string[] = [];
  for (const tag of lists.flat()) {
    const folded = tag.toLowerCase();
    if (seen.has(folded)) continue;
    seen.add(folded);
    merged.push(tag);
  }
  return merged;
}

/** A `{FR}` marker in a folder or file name. */
function braceLanguage(name: string): Language | null {
  for (const match of name.matchAll(BRACE_GROUP)) {
    const code = match[1].toUpperCase();
    if (LANGUAGE_CODES.has(code)) return code as Language;
  }
  return null;
}

function stripGroups(name: string): string {
  return name.replace(ANY_GROUP, " ").replace(/\s+/g, " ").trim();
}

export interface FolderInfo {
  tags: string[];
  lang: Language | null;
}

/**
 * A folder's own tags and language (null when it names none). Language
 * comes from a `{FR}` marker, a name that is a language (`French`, `FR`),
 * or an upper-case code leading the name (`PT - CARD`).
 */
export function parseFolder(rawName: string): FolderInfo {
  const name = stripSortPrefix(rawName);
  let lang = braceLanguage(name);
  if (!lang) {
    const bare = stripGroups(name);
    lang = LANGUAGE_NAMES[bare.toLowerCase()] ?? null;
    const leading = LEADING_CODE.exec(bare);
    if (!lang && leading && LANGUAGE_CODES.has(leading[1])) lang = leading[1] as Language;
  }
  return { tags: bracketTags(name), lang };
}

export interface FileNameInfo {
  name: string;
  ext: "png" | "jpg" | "jpeg";
  tags: string[];
  lang: Language | null;
}

const MIME_EXT: Record<string, "png" | "jpg"> = {
  "image/png": "png",
  "image/jpeg": "jpg",
};

/**
 * The card name, extension, tags and language marker of an image file, or
 * null for a file that isn't a PNG or JPEG.
 */
export function parseFileName(rawName: string, mimeType: string): FileNameInfo | null {
  const mimeExt = MIME_EXT[mimeType];
  if (!mimeExt) return null;
  let name = stripSortPrefix(rawName);
  let ext: FileNameInfo["ext"] = mimeExt;
  const extMatch = IMAGE_EXT.exec(name);
  if (extMatch) {
    ext = extMatch[1].toLowerCase() as FileNameInfo["ext"];
    name = name.slice(0, extMatch.index);
  }
  let cardName = stripGroups(name).replace(UNCLOSED_GROUP, "").trim();
  for (let prev = ""; prev !== cardName; ) {
    prev = cardName;
    cardName = cardName.replace(TRAILING_SUFFIX, "").trim();
  }
  // Separator debris only; underscores inside the name (`Mishra_s`) stay.
  cardName = cardName.replace(/^[\s\-–—_]+|[\s\-–—_]+$/g, "").trim();
  if (!cardName) return null;
  return { name: cardName, ext, tags: bracketTags(name), lang: braceLanguage(name) };
}

/** What a folder passes down to everything below it. */
export interface FolderContext {
  /** Raw folder names below the source root. */
  path: string[];
  /** Inherited tags, outermost folder first. */
  tags: string[];
  /** The nearest folder's language, if any folder named one. */
  lang: Language | null;
}

export const ROOT_CONTEXT: FolderContext = { path: [], tags: [], lang: null };

export function childContext(parent: FolderContext, rawName: string): FolderContext {
  const info = parseFolder(rawName);
  return {
    path: [...parent.path, rawName],
    tags: mergeTags(parent.tags, info.tags),
    lang: info.lang ?? parent.lang,
  };
}

function toInt(value: unknown): number {
  const n = typeof value === "number" ? value : Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
}

/** A shard record for one Drive file, or null if it isn't a usable image. */
export function buildRecord(file: DriveFile, ctx: FolderContext): CatalogRecord | null {
  if (!file.id || !file.name) return null;
  const parsed = parseFileName(file.name, file.mimeType);
  if (!parsed) return null;
  const w = toInt(file.imageMediaMetadata?.width);
  const h = toInt(file.imageMediaMetadata?.height);
  return {
    id: file.id,
    name: parsed.name,
    ext: parsed.ext,
    type: cardType(ctx.path),
    lang: parsed.lang ?? ctx.lang ?? DEFAULT_LANGUAGE,
    tags: mergeTags(ctx.tags, parsed.tags),
    dpi: dpiFor(w, h),
    w,
    h,
    size: toInt(file.size),
    created: file.createdTime ?? "",
    modified: file.modifiedTime ?? "",
    folder: ctx.path.join("/"),
  };
}
