/**
 * Deletes the run's temp crawl objects, writes manifest.json from this
 * run's crawls, then deletes shards that neither the new manifest nor the
 * previous one refers to.
 */

import { runTempDir, type CrawlSummary } from "./crawl";
import type { Source } from "./sources";

export const MANIFEST_FORMAT = 1;
export const MANIFEST_PATH = "manifest.json";
const SHARDS_DIR = "sources/";
const TEMP_DIR = "tmp/";
// Temp objects another run left behind (it crashed before publishing) are
// deleted once they're this old; younger ones may belong to a live run.
const STALE_TEMP_MS = 2 * 24 * 60 * 60 * 1000;

export interface ManifestSource {
  key: string;
  name: string;
  order: number;
  path: string;
  sha256: string;
  bytes: number;
  count: number;
  crawled_at: string;
}

export interface Manifest {
  format: number;
  version: string;
  generated_at: string;
  /** How many sources MPC Fill lists in all, public folder or not. */
  source_total: number;
  sources: ManifestSource[];
}

export interface PublishResult {
  version: string;
  sources: number;
  crawled: number;
  kept: string[];
  dropped: string[];
  deleted: number;
  temp_deleted: number;
}

/** The previous manifest's entries by key; empty if it's missing or unreadable. */
export async function readPreviousSources(
  bucket: R2Bucket,
  prefix: string,
): Promise<Map<string, ManifestSource>> {
  const object = await bucket.get(prefix + MANIFEST_PATH);
  if (!object) return new Map();
  try {
    const manifest = (await object.json()) as Partial<Manifest>;
    if (manifest.format !== MANIFEST_FORMAT || !Array.isArray(manifest.sources)) return new Map();
    return new Map(manifest.sources.map((s) => [s.key, s]));
  } catch {
    console.warn("previous manifest.json is unreadable; starting fresh");
    return new Map();
  }
}

/**
 * The new manifest's sources, in MPC Fill's order. A source not crawled
 * this run (it failed, or a manual run left it out) keeps its previous
 * entry, renumbered only in `order`; with no previous entry it's left out.
 */
export function buildSources(
  sources: readonly Source[],
  crawled: ReadonlyMap<string, CrawlSummary>,
  previous: ReadonlyMap<string, ManifestSource>,
): { entries: ManifestSource[]; kept: string[]; dropped: string[] } {
  const entries: ManifestSource[] = [];
  const kept: string[] = [];
  const dropped: string[] = [];
  for (const source of sources) {
    const fresh = crawled.get(source.key);
    if (fresh) {
      entries.push({
        key: fresh.key,
        name: fresh.name,
        order: entries.length,
        path: fresh.path,
        sha256: fresh.sha256,
        bytes: fresh.bytes,
        count: fresh.count,
        crawled_at: fresh.crawled_at,
      });
      continue;
    }
    const old = previous.get(source.key);
    if (old) {
      entries.push({ ...old, order: entries.length });
      kept.push(source.key);
    } else {
      dropped.push(source.key);
    }
  }
  return { entries, kept, dropped };
}

export interface PublishOptions {
  bucket: R2Bucket;
  prefix: string;
  instanceId: string;
  version: string;
  generatedAt: string;
  sources: readonly Source[];
  /** Every source MPC Fill lists, Drive-linked or not. */
  sourceTotal: number;
  crawled: readonly CrawlSummary[];
}

export async function publish(options: PublishOptions): Promise<PublishResult> {
  const { bucket, prefix, version } = options;
  // First, so a run that publishes nothing still cleans up after itself.
  const tempDeleted = await deleteTempObjects(bucket, prefix, options.instanceId);
  const previous = await readPreviousSources(bucket, prefix);
  const crawled = new Map(options.crawled.map((c) => [c.key, c]));
  const { entries, kept, dropped } = buildSources(options.sources, crawled, previous);
  if (entries.length === 0) {
    // Publishing nothing would wipe every server's catalogue.
    throw new Error("no sources to publish; leaving the previous manifest in place");
  }
  const manifest: Manifest = {
    format: MANIFEST_FORMAT,
    version,
    generated_at: options.generatedAt,
    source_total: Math.max(options.sourceTotal, entries.length),
    sources: entries,
  };
  await bucket.put(prefix + MANIFEST_PATH, JSON.stringify(manifest, null, 1), {
    httpMetadata: { contentType: "application/json", cacheControl: "public, max-age=300" },
  });
  const referenced = new Set<string>([
    ...entries.map((s) => prefix + s.path),
    ...[...previous.values()].map((s) => prefix + s.path),
  ]);
  const deleted = await deleteUnreferencedShards(bucket, prefix + SHARDS_DIR, referenced);
  for (const key of kept) console.log(`${key}: not crawled this run; kept its previous entry`);
  for (const key of dropped) console.log(`${key}: not crawled and no previous entry; left out`);
  console.log(`published ${version}: ${entries.length} sources, ${deleted} old shards deleted`);
  return {
    version,
    sources: entries.length,
    crawled: crawled.size,
    kept,
    dropped,
    deleted,
    temp_deleted: tempDeleted,
  };
}

/** This run's temp objects, plus any stale ones from runs that never published. */
async function deleteTempObjects(bucket: R2Bucket, prefix: string, instanceId: string): Promise<number> {
  const mine = runTempDir(prefix, instanceId);
  const staleBefore = Date.now() - STALE_TEMP_MS;
  return deleteWhere(
    bucket,
    prefix + TEMP_DIR,
    (object) => object.key.startsWith(mine) || object.uploaded.getTime() < staleBefore,
  );
}

function deleteUnreferencedShards(
  bucket: R2Bucket,
  dir: string,
  referenced: ReadonlySet<string>,
): Promise<number> {
  return deleteWhere(bucket, dir, (object) => !referenced.has(object.key));
}

async function deleteWhere(
  bucket: R2Bucket,
  dir: string,
  doomedIf: (object: R2Object) => boolean,
): Promise<number> {
  const doomed: string[] = [];
  let cursor: string | undefined;
  do {
    const page = await bucket.list({ prefix: dir, cursor });
    for (const object of page.objects) {
      if (doomedIf(object)) doomed.push(object.key);
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  // R2 deletes up to 1000 keys per call.
  for (let i = 0; i < doomed.length; i += 1000) {
    await bucket.delete(doomed.slice(i, i + 1000));
  }
  return doomed.length;
}
