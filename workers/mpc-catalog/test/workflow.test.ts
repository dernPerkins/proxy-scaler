import { introspectWorkflowInstance } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { afterEach, describe, expect, it, vi } from "vitest";

import { USER_AGENT } from "../src/http";
import type { CrawlResult } from "../src/index";
import type { Manifest } from "../src/publish";
import { gzip, sha256Hex, toNdjson } from "../src/crawl";

const FOLDER = "application/vnd.google-apps.folder";

// ---- a fake mpcfill.com and Drive ----------------------------------------

interface FakeFile {
  id: string;
  name: string;
  mimeType: string;
  size?: string;
  createdTime?: string;
  modifiedTime?: string;
  imageMediaMetadata?: { width: number; height: number };
}

const image = (id: string, name: string, w = 3288, h = 4488): FakeFile => ({
  id,
  name,
  mimeType: name.endsWith(".png") ? "image/png" : "image/jpeg",
  size: "1000",
  createdTime: "2024-01-02T03:04:05.000Z",
  modifiedTime: "2024-01-03T03:04:05.000Z",
  imageMediaMetadata: { width: w, height: h },
});
const folder = (id: string, name: string): FakeFile => ({ id, name, mimeType: FOLDER });

const ROOT_A = "rootAAAAAAAAAAAAAAAA";
const ROOT_B = "rootBBBBBBBBBBBBBBBB";
const ROOT_C = "rootCCCCCCCCCCCCCCCC";

/** Folder id → its children. */
type DriveTree = Record<string, FakeFile[]>;

function treeA(): DriveTree {
  // 1203 images at the root: two pages of 1000 plus one more.
  const many = Array.from({ length: 1203 }, (_, i) => image(`a-root-${i}`, `Card ${i}.png`));
  return {
    [ROOT_A]: [
      folder("a-mursu", "!Mursu [Retro, Gold Border]"),
      folder("a-removed", "!Removed"),
      folder("a-tokens", "{FR} Tokens"),
      { id: "a-notes", name: "notes.txt", mimeType: "text/plain" },
      ...many,
    ],
    "a-mursu": [folder("a-green", "Green"), image("a-bolt", "Lightning Bolt [Extended].png")],
    "a-green": [image("a-elf", "Llanowar Elves (M19) [retro].jpg", 1500, 2100)],
    "a-tokens": [image("a-angel", "Angel (Token) [RVR] {3} (1).jpg")],
    "a-removed": [image("a-gone", "Gone.png")],
  };
}

function treeC(): DriveTree {
  return {
    [ROOT_C]: [folder("c-backs", "Cardbacks"), folder("c-old", "Old Stuff")],
    "c-backs": [image("c-back", "Black Lotus Back.png")],
    "c-old": [image("c-older", "Ancient.png")],
  };
}

const SOURCES = {
  results: {
    "1": { pk: 1, key: "Hidden", name: "Hidden", externalLink: "", sourceType: "Google Drive" },
    "2": {
      pk: 2,
      key: "Alpha Source",
      name: "Alpha Source",
      externalLink: `https://drive.google.com/open?id=${ROOT_A}`,
      sourceType: "Google Drive",
    },
    "3": {
      pk: 3,
      key: "Beta",
      name: "Beta",
      externalLink: `https://drive.google.com/open?id=${ROOT_B}`,
      sourceType: "Google Drive",
    },
    "4": {
      pk: 4,
      key: "Gamma",
      name: "Gamma",
      externalLink: `https://drive.google.com/drive/folders/${ROOT_C}`,
      sourceType: "Google Drive",
    },
  },
};

interface FakeOptions {
  tree: DriveTree;
  /** Folder ids whose listing always fails with this status. */
  failing?: Record<string, number>;
  /** Respond 429 to the first request for each of these folder ids. */
  throttleOnce?: string[];
  /** Respond 500 to the first N requests for each of these folder ids. */
  failTimes?: Record<string, number>;
  /** Never answer the first request for each of these folder ids. */
  hangOnce?: string[];
}

const PAGE = 1000;

function installFakes({ tree, failing = {}, throttleOnce = [], failTimes = {}, hangOnce = [] }: FakeOptions) {
  const throttled = new Set<string>();
  const hung = new Set<string>();
  const failures: Record<string, number> = {};
  const calls: { url: URL; ua: string | null }[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const request = new Request(input as RequestInfo, init);
    const url = new URL(request.url);
    calls.push({ url, ua: request.headers.get("User-Agent") });
    if (url.href === "https://mpcfill.com/2/sources/") return Response.json(SOURCES);
    if (url.origin + url.pathname !== "https://www.googleapis.com/drive/v3/files") {
      return new Response("unexpected", { status: 500 });
    }
    expect(url.searchParams.get("key")).toBe("test-key");
    expect(url.searchParams.get("pageSize")).toBe("1000");
    const id = /^'([^']+)' in parents and trashed=false$/.exec(url.searchParams.get("q") ?? "")?.[1] ?? "";
    if (failing[id]) {
      return Response.json({ error: { code: failing[id], message: "nope" } }, { status: failing[id] });
    }
    if ((failures[id] ?? 0) < (failTimes[id] ?? 0)) {
      failures[id] = (failures[id] ?? 0) + 1;
      return new Response("oops", { status: 500 });
    }
    if (hangOnce.includes(id) && !hung.has(id)) {
      hung.add(id);
      // Answers only by rejecting when the caller's timeout aborts it.
      return new Promise<Response>((_, reject) => {
        const signal = init?.signal ?? request.signal;
        signal?.addEventListener("abort", () => reject(signal.reason));
      });
    }
    if (throttleOnce.includes(id) && !throttled.has(id)) {
      throttled.add(id);
      return new Response("slow down", { status: 429 });
    }
    const children = tree[id];
    if (!children) return Response.json({ error: { code: 404 } }, { status: 404 });
    const start = Number(url.searchParams.get("pageToken") ?? 0);
    const files = children.slice(start, start + PAGE);
    const next = start + PAGE < children.length ? String(start + PAGE) : undefined;
    return Response.json({ files, ...(next ? { nextPageToken: next } : {}) });
  });
  /** How many times each folder id was listed (all pages and attempts). */
  const listings = () => {
    const counts: Record<string, number> = {};
    for (const { url } of calls) {
      const id = /^'([^']+)'/.exec(url.searchParams.get("q") ?? "")?.[1];
      if (id) counts[id] = (counts[id] ?? 0) + 1;
    }
    return counts;
  };
  return { spy, calls, listings };
}

// ---- helpers ---------------------------------------------------------------

let runs = 0;

type Introspector = Awaited<ReturnType<typeof introspectWorkflowInstance>>;

async function runCrawl(
  params: Record<string, unknown> = {},
  inspect?: (instance: Introspector) => Promise<void>,
): Promise<CrawlResult> {
  const id = `run-${++runs}`;
  const instance = await introspectWorkflowInstance(env.CATALOG_CRAWL, id);
  try {
    // Step retries still run, just without the minutes-long waits.
    await instance.modify(async (m) => {
      await m.disableRetryDelays();
    });
    await env.CATALOG_CRAWL.create({ id, params });
    await instance.waitForStatus("complete");
    await inspect?.(instance);
    return (await instance.getOutput()) as CrawlResult;
  } finally {
    await instance.dispose();
  }
}

async function readManifest(prefix = ""): Promise<Manifest> {
  const object = await env.CATALOG.get(`${prefix}manifest.json`);
  expect(object).not.toBeNull();
  return (await object!.json()) as Manifest;
}

async function readShard(path: string): Promise<{ bytes: Uint8Array; records: Record<string, unknown>[] }> {
  const object = await env.CATALOG.get(path);
  expect(object).not.toBeNull();
  const bytes = new Uint8Array(await object!.arrayBuffer());
  const text = await new Response(
    new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip")),
  ).text();
  const records = text
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line) as Record<string, unknown>);
  return { bytes, records };
}

async function bucketKeys(): Promise<string[]> {
  const listed = await env.CATALOG.list();
  return listed.objects.map((o) => o.key).sort();
}

afterEach(async () => {
  vi.restoreAllMocks();
  const listed = await env.CATALOG.list();
  await Promise.all(listed.objects.map((o) => env.CATALOG.delete(o.key)));
});

// ---- tests -----------------------------------------------------------------

describe("CatalogCrawl", () => {
  it("crawls every Drive-linked source and publishes a manifest", async () => {
    const tree = { ...treeA(), ...treeC(), [ROOT_B]: [image("b-1", "Sol Ring.png")] };
    const { calls } = installFakes({ tree, throttleOnce: ["a-mursu"] });

    const result = await runCrawl();
    expect(result.failed).toEqual([]);
    expect(result.sources).toBe(3);

    // One MPC Fill call, and every request identifies itself.
    expect(calls.filter((c) => c.url.hostname === "mpcfill.com")).toHaveLength(1);
    expect(new Set(calls.map((c) => c.ua))).toEqual(new Set([USER_AGENT]));
    expect(USER_AGENT).toMatch(/^proxy-scaler-catalog\/\d+\.\d+\.\d+ \(\+https:\/\/proxy-scaler\.com\)$/);
    // The discard pile is never even listed.
    const listed = calls.map((c) => c.url.searchParams.get("q") ?? "");
    expect(listed.some((q) => q.includes("a-removed"))).toBe(false);
    expect(listed.some((q) => q.includes("c-old"))).toBe(false);

    const manifest = await readManifest();
    expect(manifest.format).toBe(1);
    expect(manifest.version).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
    expect(manifest.generated_at >= manifest.version).toBe(true);
    expect(manifest.sources.map((s) => [s.key, s.name, s.order])).toEqual([
      ["Alpha Source", "Alpha Source", 0],
      ["Beta", "Beta", 1],
      ["Gamma", "Gamma", 2],
    ]);

    for (const entry of manifest.sources) {
      // Content-addressed, with only server-safe characters.
      expect(entry.path).toMatch(/^sources\/[A-Za-z0-9._-]+\.[0-9a-f]{64}\.ndjson\.gz$/);
      expect(entry.path).toBe(`sources/${entry.key.replace(" ", "_")}.${entry.sha256}.ndjson.gz`);
      const { bytes, records } = await readShard(entry.path);
      expect(entry.sha256).toBe(await sha256Hex(bytes));
      expect(entry.bytes).toBe(bytes.byteLength);
      expect(entry.count).toBe(records.length);
      expect(entry.crawled_at).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
    }

    const alpha = await readShard(manifest.sources[0].path);
    expect(alpha.records).toHaveLength(1203 + 3);
    const byId = new Map(alpha.records.map((r) => [r.id, r]));
    expect(byId.has("a-gone")).toBe(false);
    expect(byId.has("a-notes")).toBe(false);
    expect(byId.get("a-elf")).toEqual({
      id: "a-elf",
      name: "Llanowar Elves",
      ext: "jpg",
      type: "CARD",
      lang: "EN",
      tags: ["Retro", "Gold Border"],
      dpi: 565,
      w: 1500,
      h: 2100,
      size: 1000,
      created: "2024-01-02T03:04:05.000Z",
      modified: "2024-01-03T03:04:05.000Z",
      folder: "!Mursu [Retro, Gold Border]/Green",
    });
    expect(byId.get("a-bolt")).toMatchObject({ tags: ["Retro", "Gold Border", "Extended"], dpi: 1208 });
    expect(byId.get("a-angel")).toMatchObject({ name: "Angel", type: "TOKEN", lang: "FR", tags: [] });
    expect(byId.get("a-root-1202")).toMatchObject({ name: "Card 1202", folder: "" });

    const gamma = await readShard(manifest.sources[2].path);
    expect(gamma.records.map((r) => [r.id, r.type])).toEqual([["c-back", "CARDBACK"]]);
  });

  it("writes the same bytes for the same Drive contents", async () => {
    const tree = { ...treeA(), ...treeC(), [ROOT_B]: [image("b-1", "Sol Ring.png")] };
    installFakes({ tree });
    await runCrawl();
    const first = await readManifest();
    vi.restoreAllMocks();
    // Drive returns the same files in a different order.
    const shuffled = Object.fromEntries(Object.entries(tree).map(([k, v]) => [k, [...v].reverse()]));
    installFakes({ tree: shuffled });
    const result = await runCrawl();
    const second = await readManifest();
    expect(second.version >= first.version).toBe(true);
    expect(second.sources.map((s) => s.sha256)).toEqual(first.sources.map((s) => s.sha256));
    expect(result.deleted).toBe(0);
  });

  it("keeps a failing source's previous entry, then garbage-collects old shards", async () => {
    // Run 1: everything works.
    installFakes({ tree: { ...treeA(), ...treeC(), [ROOT_B]: [image("b-1", "Sol Ring.png")] } });
    await runCrawl();
    const run1 = await readManifest();
    const [alpha1, beta1, gamma1] = run1.sources;
    vi.restoreAllMocks();

    // Run 2: Beta's folder 404s (a NonRetryableError) and Gamma's lists
    // keep failing with 500 until the step's retries run out. Alpha changes.
    installFakes({
      tree: { ...treeA(), "a-green": [image("a-elf2", "Llanowar Elves.png")] },
      failing: { [ROOT_B]: 404, [ROOT_C]: 500 },
    });
    const result2 = await runCrawl();
    expect(result2.failed).toEqual(["Beta", "Gamma"]);
    expect(result2.kept).toEqual(["Beta", "Gamma"]);
    const run2 = await readManifest();
    const [alpha2, beta2, gamma2] = run2.sources;
    expect(alpha2.sha256).not.toBe(alpha1.sha256);
    expect(beta2).toEqual(beta1);
    expect(gamma2).toEqual(gamma1);
    // Both of Alpha's shards are still referenced (new + previous manifest).
    expect(await bucketKeys()).toEqual(
      ["manifest.json", alpha1.path, alpha2.path, beta1.path, gamma1.path].sort(),
    );
    vi.restoreAllMocks();

    // Run 3: Alpha changes again and an orphan shard from some aborted run
    // is lying around; neither manifest refers to alpha1 or the orphan.
    await env.CATALOG.put("sources/Orphan.0000.ndjson.gz", "x");
    installFakes({
      tree: { ...treeA(), ...treeC(), [ROOT_B]: [image("b-1", "Sol Ring.png")], "a-green": [] },
    });
    const result3 = await runCrawl();
    expect(result3.failed).toEqual([]);
    expect(result3.deleted).toBe(2);
    const run3 = await readManifest();
    expect(run3.sources[1]).toEqual({ ...beta1, crawled_at: run3.sources[1].crawled_at });
    expect(await bucketKeys()).toEqual(
      ["manifest.json", alpha2.path, ...run3.sources.map((s) => s.path)]
        .filter((v, i, all) => all.indexOf(v) === i)
        .sort(),
    );
  });

  it("leaves out a failing source that has no previous entry", async () => {
    installFakes({ tree: { ...treeC(), [ROOT_B]: [image("b-1", "Sol Ring.png")] }, failing: { [ROOT_A]: 403 } });
    const result = await runCrawl();
    expect(result.failed).toEqual(["Alpha Source"]);
    expect(result.dropped).toEqual(["Alpha Source"]);
    expect((await readManifest()).sources.map((s) => [s.key, s.order])).toEqual([
      ["Beta", 0],
      ["Gamma", 1],
    ]);
  });

  it("writes a dry run under the prefix and touches nothing else", async () => {
    await env.CATALOG.put("manifest.json", "{}");
    await env.CATALOG.put("sources/Live.abc.ndjson.gz", "live");
    installFakes({ tree: { ...treeC(), [ROOT_B]: [image("b-1", "Sol Ring.png")] } });
    const result = await runCrawl({ dryRun: true, only: ["Beta"] });
    expect(result.sources).toBe(1);
    const manifest = await readManifest("dev/");
    expect(manifest.sources.map((s) => s.key)).toEqual(["Beta"]);
    expect(await bucketKeys()).toEqual(
      ["dev/manifest.json", `dev/${manifest.sources[0].path}`, "manifest.json", "sources/Live.abc.ndjson.gz"].sort(),
    );
  });
});

describe("chunked crawls", () => {
  // Beta: a root, a chain of folders and a few side folders, so with two
  // folder listings per chunk (vitest.config.ts) it spans three chunks.
  function deepTree(): DriveTree {
    return {
      [ROOT_B]: [folder("b-1", "One [Retro]"), folder("b-2", "Two"), image("b-root", "Root.png")],
      "b-1": [folder("b-1a", "Deep"), image("b-1-img", "Alpha.png")],
      "b-2": [folder("b-2a", "{FR} Deeper"), folder("b-1", "One [Retro]"), image("b-2-img", "Beta.png")],
      "b-1a": [folder("b-1a-x", "Deepest"), image("b-1a-img", "Gamma.png")],
      // Not card images: an Arena texture (square) and a low-res card.
      "b-2a": [image("b-2a-img", "Delta.png"), image("b-tex", "418951_AIF.png", 1024, 1024), image("b-tiny", "Tiny.png", 300, 420)],
      "b-1a-x": [image("b-1a-x-img", "Epsilon.png")],
    };
  }
  const BETA_FOLDERS = [ROOT_B, "b-1", "b-2", "b-1a", "b-2a", "b-1a-x"];

  it("spans several steps and lists each folder once", async () => {
    // A temp object from some other live run must survive this run's cleanup.
    await env.CATALOG.put("tmp/other-run/Beta/state-1.json", "{}");
    const log = vi.spyOn(console, "log");
    const { listings } = installFakes({ tree: deepTree() });
    const progress: unknown[] = [];
    const result = await runCrawl({ only: ["Beta"] }, async (instance) => {
      for (const n of [0, 1, 2]) progress.push(await instance.waitForStepResult({ name: `crawl:Beta:${n}` }));
    });
    expect(result.failed).toEqual([]);
    expect(progress[0]).toEqual({ done: false, progress: { folders: 2, queued: 2, requests: 2, images: 1 + 1 } });
    expect(progress[1]).toEqual({ done: false, progress: { folders: 4, queued: 2, requests: 4, images: 4 } });
    expect(progress[2]).toMatchObject({ done: true, summary: { key: "Beta", count: 6, folders: 6, requests: 6, chunks: 3, skipped_shape: 2 } });
    expect(listings()).toEqual(Object.fromEntries(BETA_FOLDERS.map((id) => [id, 1])));
    expect(log.mock.calls.some(([line]) => /^Beta: \d+ folders done, \d+ queued, \d+ requests, \d+ images$/.test(String(line)))).toBe(true);

    const manifest = await readManifest();
    const { records } = await readShard(manifest.sources[0].path);
    const byId = new Map(records.map((r) => [r.id, r]));
    expect([...byId.keys()].sort()).toEqual(["b-1-img", "b-1a-img", "b-1a-x-img", "b-2-img", "b-2a-img", "b-root"]);
    expect(byId.get("b-1a-x-img")).toMatchObject({ folder: "One [Retro]/Deep/Deepest", tags: ["Retro"] });
    expect(byId.get("b-2a-img")).toMatchObject({ folder: "Two/{FR} Deeper", lang: "FR" });
    // This run's temp objects are gone; the other run's are not.
    expect((await bucketKeys()).filter((k) => k.startsWith("tmp/"))).toEqual(["tmp/other-run/Beta/state-1.json"]);
  });

  it("gives the same shard however the crawl is chunked", async () => {
    installFakes({ tree: deepTree() });
    await runCrawl({ only: ["Beta"] });
    const chunked = (await readManifest()).sources[0].sha256;
    const records = await readShard((await readManifest()).sources[0].path);
    expect(await sha256Hex(await gzip(toNdjson(records.records as never)))).toBe(chunked);
  });

  it("retries only the failed chunk, without re-listing finished folders", async () => {
    // b-1a is listed in chunk 1. Its first six requests fail, which uses up
    // the fetcher's attempts and fails that step once; the step retry works.
    const { listings } = installFakes({ tree: deepTree(), failTimes: { "b-1a": 6 } });
    const result = await runCrawl({ only: ["Beta"] });
    expect(result.failed).toEqual([]);
    const counts = listings();
    // Chunk 0's folders were listed once. Chunk 1 ran twice: b-2 (listed
    // before the failure) twice, b-1a six failures plus the good one.
    expect(counts).toEqual({ [ROOT_B]: 1, "b-1": 1, "b-2": 2, "b-1a": 7, "b-2a": 1, "b-1a-x": 1 });
    const { records } = await readShard((await readManifest()).sources[0].path);
    expect(records).toHaveLength(6);
  });

  it("times out a request that never answers and retries it", async () => {
    const { listings } = installFakes({ tree: deepTree(), hangOnce: ["b-2"] });
    const result = await runCrawl({ only: ["Beta"] });
    expect(result.failed).toEqual([]);
    expect(listings()["b-2"]).toBe(2);
    const { records } = await readShard((await readManifest()).sources[0].path);
    expect(records).toHaveLength(6);
  });
});

describe("shards", () => {
  it("gzip output is deterministic", async () => {
    const text = toNdjson([]) + '{"id":"x"}\n';
    expect(await sha256Hex(await gzip(text))).toBe(await sha256Hex(await gzip(text)));
  });
});
