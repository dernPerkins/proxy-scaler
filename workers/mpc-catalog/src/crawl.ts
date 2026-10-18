/**
 * Crawls one source's Drive folder tree breadth-first and writes its shard:
 * gzipped NDJSON at a content-addressed path, so an unchanged source keeps
 * the same path and hash from run to run.
 *
 * A big source has thousands of folders, so its crawl is split into chunks
 * of a bounded number of folder listings, one Workflow step each. Between
 * chunks the BFS state and the records found so far live in R2 under a
 * run-scoped temp prefix (step outputs are capped at 1 MiB). Chunk n reads
 * `state-n` and writes `records-n` and `state-(n+1)`, so a retry repeats
 * only that chunk. The chunk that empties the queue assembles the shard.
 */

import type { Fetcher } from "./http";
import {
  FOLDER_MIME,
  ROOT_CONTEXT,
  buildRecord,
  childContext,
  isCardShaped,
  isDiscardFolder,
  type CatalogRecord,
  type DriveFile,
  type FolderContext,
} from "./parse";
import type { Source } from "./sources";

export const DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files";
const DRIVE_FIELDS =
  "nextPageToken,files(id,name,mimeType,size,createdTime,modifiedTime,imageMediaMetadata(width,height))";
// A runaway guard; the biggest real sources have a few thousand folders.
export const MAX_FOLDERS = 50_000;
// A chunk stops starting new folder listings after this long.
export const CHUNK_WALL_MS = 10 * 60 * 1000;
const PROGRESS_EVERY_FOLDERS = 50;
// Skipped folder paths beyond this many are counted but not listed.
const MAX_SKIPPED_LISTED = 50;

/** What a source's last step returns to the Workflow (kept small: steps persist it). */
export interface CrawlSummary {
  key: string;
  name: string;
  path: string;
  sha256: string;
  bytes: number;
  count: number;
  crawled_at: string;
  folders: number;
  requests: number;
  chunks: number;
  skipped_folders: number;
  skipped: string[];
  /** Images left out for not being card-shaped (or under MIN_DPI). */
  skipped_shape: number;
}

export interface CrawlProgress {
  folders: number;
  queued: number;
  requests: number;
  images: number;
}

/** A chunk step's result: more to do, or the finished source. */
export type ChunkResult = { done: false; progress: CrawlProgress } | { done: true; summary: CrawlSummary };

interface QueuedFolder {
  id: string;
  ctx: FolderContext;
}

/** The BFS state carried from one chunk to the next. */
interface CrawlState {
  queue: QueuedFolder[];
  /** Folder ids already queued or skipped (a folder can have several parents). */
  seen: string[];
  folders: number;
  requests: number;
  images: number;
  skipped: string[];
  skippedCount: number;
  skippedShape: number;
}

interface DriveList {
  nextPageToken?: string;
  files?: DriveFile[];
}

export function filesListUrl(folderId: string, apiKey: string, pageToken?: string): string {
  const params = new URLSearchParams({
    q: `'${folderId}' in parents and trashed=false`,
    pageSize: "1000",
    fields: DRIVE_FIELDS,
    supportsAllDrives: "true",
    includeItemsFromAllDrives: "true",
    key: apiKey,
  });
  if (pageToken) params.set("pageToken", pageToken);
  return `${DRIVE_FILES_URL}?${params}`;
}

async function listChildren(fetcher: Fetcher, folderId: string, apiKey: string): Promise<DriveFile[]> {
  const files: DriveFile[] = [];
  let pageToken: string | undefined;
  do {
    const page = await fetcher.getJson<DriveList>(filesListUrl(folderId, apiKey, pageToken));
    files.push(...(page.files ?? []));
    pageToken = page.nextPageToken || undefined;
  } while (pageToken);
  return files;
}

/**
 * Sorted by folder, name and id, with any file listed twice (it sits in two
 * folders) kept once, so the same Drive contents always give the same bytes.
 */
export function toNdjson(records: CatalogRecord[]): string {
  const sorted = [...records].sort(
    (a, b) => cmp(a.folder, b.folder) || cmp(a.name, b.name) || cmp(a.id, b.id),
  );
  const seen = new Set<string>();
  let out = "";
  for (const record of sorted) {
    if (seen.has(record.id)) continue;
    seen.add(record.id);
    out += JSON.stringify(record) + "\n";
  }
  return out;
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

export async function gzip(text: string): Promise<Uint8Array> {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Generation servers accept only `[A-Za-z0-9._-]` in shard file names. */
export function shardKey(sourceKey: string): string {
  return sourceKey.replace(/[^A-Za-z0-9._-]/g, "_");
}

export function shardPath(sourceKey: string, sha256: string): string {
  return `sources/${shardKey(sourceKey)}.${sha256}.ndjson.gz`;
}

export const SHARD_HTTP_METADATA: R2HTTPMetadata = {
  contentType: "application/gzip",
  cacheControl: "public, max-age=31536000, immutable",
};

/** Where a run keeps its in-progress crawls, below the output prefix. */
export function runTempDir(prefix: string, instanceId: string): string {
  return `${prefix}tmp/${encodeURIComponent(instanceId)}/`;
}

function sourceTempDir(prefix: string, instanceId: string, sourceKey: string): string {
  // Encoded rather than shardKey()d, so `Zukus 2` and `Zukus_2` can't collide.
  return `${runTempDir(prefix, instanceId)}${encodeURIComponent(sourceKey)}/`;
}

export interface ChunkOptions {
  bucket: R2Bucket;
  /** Object-key prefix ("" or the dry-run prefix). */
  prefix: string;
  /** The Workflow instance id, which scopes the temp objects. */
  instanceId: string;
  apiKey: string;
  fetcher: Fetcher;
  /** Folder listings per chunk. */
  chunkFolders: number;
  chunkWallMs?: number;
  now?: () => Date;
}

/**
 * Runs chunk `n` of a source's crawl: up to `chunkFolders` folder listings
 * (or CHUNK_WALL_MS), starting from the state chunk n-1 left. Reruns of the
 * same chunk read the same state and overwrite the same objects.
 */
export async function crawlChunk(source: Source, n: number, options: ChunkOptions): Promise<ChunkResult> {
  const { bucket, apiKey, fetcher } = options;
  const dir = sourceTempDir(options.prefix, options.instanceId, source.key);
  const state = n === 0 ? initialState(source) : await readState(bucket, `${dir}state-${n}.json`);
  const seen = new Set(state.seen);
  const records: CatalogRecord[] = [];
  const started = Date.now();
  const wallMs = options.chunkWallMs ?? CHUNK_WALL_MS;
  const requestsBefore = fetcher.requests;
  let listed = 0;

  while (state.queue.length > 0 && listed < options.chunkFolders && Date.now() - started < wallMs) {
    const { id, ctx } = state.queue[0];
    const children = await listChildren(fetcher, id, apiKey);
    state.queue.shift();
    listed++;
    state.folders++;
    for (const file of children) {
      if (file.mimeType === FOLDER_MIME) {
        if (seen.has(file.id)) continue;
        seen.add(file.id);
        if (isDiscardFolder(file.name)) {
          const path = [...ctx.path, file.name].join("/");
          console.log(`${source.key}: skipped discard folder "${path}"`);
          if (state.skipped.length < MAX_SKIPPED_LISTED) state.skipped.push(path);
          state.skippedCount++;
          continue;
        }
        if (seen.size > MAX_FOLDERS) throw new Error(`more than ${MAX_FOLDERS} folders`);
        state.queue.push({ id: file.id, ctx: childContext(ctx, file.name) });
        continue;
      }
      const record = buildRecord(file, ctx);
      if (!record) continue;
      if (isCardShaped(record.w, record.h)) records.push(record);
      else state.skippedShape++;
    }
    if (state.folders % PROGRESS_EVERY_FOLDERS === 0) {
      logProgress(source, state, fetcher.requests - requestsBefore, records.length);
    }
  }

  state.requests += fetcher.requests - requestsBefore;
  state.images += records.length;
  state.seen = [...seen];
  await bucket.put(`${dir}records-${n}.ndjson`, records.map((r) => JSON.stringify(r) + "\n").join(""));

  if (state.queue.length > 0) {
    await bucket.put(`${dir}state-${n + 1}.json`, JSON.stringify(state));
    logProgress(source, state, 0, 0);
    return {
      done: false,
      progress: { folders: state.folders, queued: state.queue.length, requests: state.requests, images: state.images },
    };
  }
  return { done: true, summary: await writeShard(source, n, state, dir, options) };
}

function initialState(source: Source): CrawlState {
  return {
    queue: [{ id: source.folderId, ctx: ROOT_CONTEXT }],
    seen: [source.folderId],
    folders: 0,
    requests: 0,
    images: 0,
    skipped: [],
    skippedCount: 0,
    skippedShape: 0,
  };
}

async function readState(bucket: R2Bucket, key: string): Promise<CrawlState> {
  const object = await bucket.get(key);
  if (!object) throw new Error(`crawl state ${key} is missing`);
  return (await object.json()) as CrawlState;
}

function logProgress(source: Source, state: CrawlState, chunkRequests: number, chunkImages: number) {
  console.log(
    `${source.key}: ${state.folders} folders done, ${state.queue.length} queued, ` +
      `${state.requests + chunkRequests} requests, ${state.images + chunkImages} images`,
  );
}

/** Joins every chunk's records into the content-addressed shard. */
async function writeShard(
  source: Source,
  lastChunk: number,
  state: CrawlState,
  dir: string,
  options: ChunkOptions,
): Promise<CrawlSummary> {
  const { bucket, prefix } = options;
  const records: CatalogRecord[] = [];
  for (let i = 0; i <= lastChunk; i++) {
    const object = await bucket.get(`${dir}records-${i}.ndjson`);
    if (!object) throw new Error(`crawl records ${dir}records-${i}.ndjson are missing`);
    for (const line of (await object.text()).split("\n")) {
      if (line) records.push(JSON.parse(line) as CatalogRecord);
    }
  }
  const crawledAt = isoSeconds((options.now ?? (() => new Date()))());
  const ndjson = toNdjson(records);
  const count = ndjson ? ndjson.split("\n").length - 1 : 0;
  const bytes = await gzip(ndjson);
  const sha256 = await sha256Hex(bytes);
  const path = shardPath(source.key, sha256);
  // Content-addressed: an object already at this path has these bytes.
  if (!(await bucket.head(prefix + path))) {
    await bucket.put(prefix + path, bytes, { httpMetadata: SHARD_HTTP_METADATA, sha256 });
  }
  console.log(
    `${source.key}: done, ${count} images in ${state.folders} folders ` +
      `(${state.skippedShape} not card-shaped), ` +
      `${state.requests} Drive requests over ${lastChunk + 1} chunks, ${bytes.byteLength} bytes`,
  );
  return {
    key: source.key,
    name: source.name,
    path,
    sha256,
    bytes: bytes.byteLength,
    count,
    crawled_at: crawledAt,
    folders: state.folders,
    requests: state.requests,
    chunks: lastChunk + 1,
    skipped_folders: state.skippedCount,
    skipped: state.skipped,
    skipped_shape: state.skippedShape,
  };
}

/** ISO 8601 to the second: `2026-10-18T06:00:12Z`. */
export function isoSeconds(date: Date): string {
  return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}
