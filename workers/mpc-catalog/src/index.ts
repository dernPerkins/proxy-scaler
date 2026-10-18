/**
 * The MPC Fill art catalogue crawler: a daily Workflow that publishes a
 * searchable listing of the MPC Fill uploaders' public Google Drive folders
 * to R2, for each generation server to import into its local SQLite.
 *
 * - `sources`: one GET of mpcfill.com/2/sources/ per run. That is the only
 *   MPC Fill traffic. Sources without a public Drive link are skipped and
 *   their hidden folder ids are never recovered.
 * - `crawl:<key>:<n>`, chunk n of a source: Drive v3 files.list,
 *   breadth-first and one request at a time, at most CRAWL_CHUNK_FOLDERS
 *   folder listings per step, with the queue and records carried between
 *   chunks in R2. The last chunk writes a gzipped NDJSON shard at a
 *   content-addressed path. A source that still fails after retries keeps
 *   its previous manifest entry.
 * - `publish`: deletes the run's temp objects, writes manifest.json, then
 *   garbage-collects shards neither the new nor the previous manifest
 *   refers to.
 *
 * Only metadata is listed. Card images never pass through this Worker or
 * our bucket; generation servers fetch them from Google themselves.
 *
 * Output format: .scratch/mpcfill-art-picker/phase-c/format.md. Why:
 * docs/adr/0006-mpc-fill-art-via-generation-server.md.
 */

import { WorkflowEntrypoint, type WorkflowEvent, type WorkflowStep } from "cloudflare:workers";
import { NonRetryableError } from "cloudflare:workflows";

import { crawlChunk, isoSeconds, type CrawlSummary } from "./crawl";
import { Fetcher, HttpError } from "./http";
import { publish, type PublishResult } from "./publish";
import { fetchSources } from "./sources";

/** Parameters for a manual run; a scheduled run has none. */
export interface CrawlParams {
  /** Write under DRY_RUN_PREFIX instead of the bucket root. */
  dryRun?: boolean;
  /** Crawl only these source keys; the rest keep their previous entries. */
  only?: string[];
}

export interface CrawlResult extends PublishResult {
  failed: string[];
}

const SOURCES_STEP = {
  retries: { limit: 3, delay: "5 minutes", backoff: "exponential" },
  timeout: "5 minutes",
} as const;
// A chunk stops starting new listings after 10 minutes; the timeout leaves
// room for the last listing's retries.
const CRAWL_STEP = {
  retries: { limit: 2, delay: "2 minutes", backoff: "exponential" },
  timeout: "30 minutes",
} as const;
// A runaway guard on chunks per source (with 300 folders each, 300,000).
const MAX_CHUNKS = 1000;
const PUBLISH_STEP = {
  retries: { limit: 3, delay: "30 seconds", backoff: "exponential" },
  timeout: "10 minutes",
} as const;

function fetcherFor(env: Env): Fetcher {
  return new Fetcher({
    gapMs: Number(env.DRIVE_GAP_MS) || 0,
    backoffMs: Number(env.DRIVE_BACKOFF_MS) || 0,
    timeoutMs: Number(env.DRIVE_TIMEOUT_MS) || 30_000,
  });
}

/** "" for a normal run; the configured dry-run prefix (ending in "/") otherwise. */
export function outputPrefix(params: CrawlParams, env: Pick<Env, "DRY_RUN_PREFIX">): string {
  if (!params.dryRun) return "";
  const prefix: string = env.DRY_RUN_PREFIX ?? "";
  if (!/^[A-Za-z0-9._-]+(\/[A-Za-z0-9._-]+)*\/$/.test(prefix)) {
    throw new NonRetryableError(`DRY_RUN_PREFIX must look like "dev/", got "${prefix}"`);
  }
  return prefix;
}

export class CatalogCrawl extends WorkflowEntrypoint<Env, CrawlParams> {
  async run(event: Readonly<WorkflowEvent<CrawlParams>>, step: WorkflowStep): Promise<CrawlResult> {
    const params: CrawlParams = event.payload ?? {};
    const prefix = outputPrefix(params, this.env);
    const version = isoSeconds(new Date(event.timestamp));

    const sources = await step.do("sources", SOURCES_STEP, async () => {
      const list = await fetchSources(fetcherFor(this.env));
      if (list.length === 0) throw new NonRetryableError("MPC Fill listed no Drive-linked sources");
      return list;
    });

    const only = params.only ? new Set(params.only) : null;
    const crawled: CrawlSummary[] = [];
    const failed: string[] = [];
    for (const source of sources) {
      if (only && !only.has(source.key)) continue;
      try {
        let summary: CrawlSummary | null = null;
        for (let n = 0; !summary; n++) {
          if (n >= MAX_CHUNKS) throw new Error(`more than ${MAX_CHUNKS} chunks`);
          const result = await step.do(`crawl:${source.key}:${n}`, CRAWL_STEP, async () => {
            if (!this.env.GOOGLE_API_KEY) throw new NonRetryableError("GOOGLE_API_KEY is not set");
            try {
              return await crawlChunk(source, n, {
                bucket: this.env.CATALOG,
                prefix,
                instanceId: event.instanceId,
                apiKey: this.env.GOOGLE_API_KEY,
                fetcher: fetcherFor(this.env),
                chunkFolders: Number(this.env.CRAWL_CHUNK_FOLDERS) || 300,
              });
            } catch (err) {
              // A 404 or a private folder won't fix itself on a step retry.
              if (err instanceof HttpError) throw new NonRetryableError(err.message);
              throw err;
            }
          });
          if (result.done) summary = result.summary;
        }
        crawled.push(summary);
      } catch (err) {
        failed.push(source.key);
        console.error(`${source.key}: crawl failed: ${(err as Error).message}`);
      }
    }

    const result = await step.do("publish", PUBLISH_STEP, () =>
      publish({
        bucket: this.env.CATALOG,
        prefix,
        instanceId: event.instanceId,
        version,
        generatedAt: isoSeconds(new Date()),
        sources,
        crawled,
      }),
    );
    return { ...result, failed };
  }
}

export default {
  // Nothing to serve: catalog.proxy-scaler.com is the bucket's own domain.
  async fetch(): Promise<Response> {
    return new Response("Not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
