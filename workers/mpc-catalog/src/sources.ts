/**
 * The source list: MPC Fill's uploaders whose public `externalLink` is a
 * Google Drive folder. Sources without a link are skipped. Their folder ids
 * are hidden on purpose, and we never try to recover them.
 */

import type { Fetcher } from "./http";

export const MPCFILL_SOURCES_URL = "https://mpcfill.com/2/sources/";

export interface Source {
  key: string;
  name: string;
  folderId: string;
}

interface RawSource {
  key?: unknown;
  name?: unknown;
  externalLink?: unknown;
}

const FOLDER_ID = /^[A-Za-z0-9_-]{10,}$/;

/**
 * The folder id from a Drive folder link: `open?id=<id>` (every link MPC
 * Fill lists today) or `/drive/folders/<id>` (also `/drive/u/0/folders/`).
 */
export function driveFolderId(link: string): string | null {
  let url: URL;
  try {
    url = new URL(link.trim());
  } catch {
    return null;
  }
  if (url.hostname !== "drive.google.com") return null;
  let id: string | null = null;
  if (url.pathname === "/open") {
    id = url.searchParams.get("id");
  } else {
    id = /^\/drive\/(?:u\/\d+\/)?folders\/([^/]+)\/?$/.exec(url.pathname)?.[1] ?? null;
  }
  return id && FOLDER_ID.test(id) ? id : null;
}

/**
 * Drive-linked sources in MPC Fill's order. `results` is keyed by pk, and
 * JavaScript iterates integer-like keys in ascending order, which is the
 * order the API sends them in.
 */
export function parseSources(body: unknown): { sources: Source[]; withoutLink: number } {
  const results = (body as { results?: unknown } | null)?.results;
  if (!results || typeof results !== "object") {
    throw new Error("MPC Fill sent an unexpected sources list");
  }
  const sources: Source[] = [];
  const seen = new Set<string>();
  let withoutLink = 0;
  for (const raw of Object.values(results as Record<string, RawSource>)) {
    if (!raw || typeof raw.key !== "string" || !raw.key) continue;
    const link = typeof raw.externalLink === "string" ? raw.externalLink : "";
    const folderId = link ? driveFolderId(link) : null;
    if (!folderId) {
      withoutLink++;
      continue;
    }
    if (seen.has(raw.key)) continue;
    seen.add(raw.key);
    sources.push({
      key: raw.key,
      name: typeof raw.name === "string" && raw.name ? raw.name : raw.key,
      folderId,
    });
  }
  return { sources, withoutLink };
}

/** The Drive-linked sources, and how many sources MPC Fill lists in all
 *  (published as the manifest's `source_total`, so the app can say how
 *  many uploaders share their folders publicly). */
export async function fetchSources(fetcher: Fetcher): Promise<{ sources: Source[]; total: number }> {
  const { sources, withoutLink } = parseSources(await fetcher.getJson(MPCFILL_SOURCES_URL));
  console.log(`MPC Fill: ${sources.length} Drive-linked sources, ${withoutLink} without a usable link`);
  return { sources, total: sources.length + withoutLink };
}
