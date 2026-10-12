import { createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { afterEach, describe, expect, it, vi } from "vitest";

import worker from "../src/index";

const UA = "proxy-scaler/0.4.0 (https://proxy-scaler.com)";
const PATH = "/png/front/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.png";
const VERSION = "1706239968";
const KEY = `png/front/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.png?${VERSION}`;
const DAY_MS = 24 * 60 * 60 * 1000;

type Upstream = (url: string) => Response | Promise<Response>;

function mockUpstream(answer: Upstream = () => new Response("fresh", { status: 200 })) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input: RequestInfo | URL) =>
      answer(input instanceof Request ? input.url : String(input)),
    );
}

async function get(
  path: string,
  { ua = UA, envOverrides = {} }: { ua?: string | null; envOverrides?: Partial<Env> } = {},
) {
  const headers = new Headers();
  if (ua !== null) headers.set("User-Agent", ua);
  const request = new Request(`https://img.proxy-scaler.com${path}`, { headers });
  const ctx = createExecutionContext();
  const response = await worker.fetch(request, { ...env, ...envOverrides } as Env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

async function seed(key: string, body: string, ageDays: number) {
  await env.IMAGES.put(key, body, {
    httpMetadata: { contentType: "image/png" },
    customMetadata: { fetchedAt: String(Date.now() - ageDays * DAY_MS) },
  });
}

afterEach(async () => {
  vi.restoreAllMocks();
  const listed = await env.IMAGES.list();
  await Promise.all(listed.objects.map((o) => env.IMAGES.delete(o.key)));
});

describe("allowlist", () => {
  it.each([
    `${PATH}?${VERSION}`,
    PATH,
    "/normal/back/a/b/ab345678-5543-42ac-9ead-0e890b188e99.jpg?1",
    "/small/front/0/0/00000000-0000-0000-0000-000000000000.jpg",
    "/large/front/0/0/00000000-0000-0000-0000-000000000000.jpg",
    "/art_crop/front/0/0/00000000-0000-0000-0000-000000000000.jpg",
    "/border_crop/front/0/0/00000000-0000-0000-0000-000000000000.jpg",
  ])("proxies %s, rebuilding the upstream URL on cards.scryfall.io", async (path) => {
    const upstream = mockUpstream();
    const response = await get(path);
    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledTimes(1);
    expect(upstream.mock.calls[0][0]).toBe(`https://cards.scryfall.io${path}`);
  });

  it.each([
    "/",
    "/favicon.ico",
    "/png/front/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.jpg", // png must be .png
    "/normal/front/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.png", // jpg sizes are .jpg
    "/png/front/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.gif",
    "/png/side/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.png",
    "/huge/front/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.jpg",
    "/png/front/7/7/not-a-uuid.png",
    "/png/front/7/7/.png",
    "/png/front/7/7/",
    "/png/front/1/2/77c6fa74-5543-42ac-9ead-0e890b188e99.png", // dirs must match the uuid
    "/png/front/7/7/../../../../etc/passwd",
    "/png/front/7/7/%2e%2e/77c6fa74-5543-42ac-9ead-0e890b188e99.png",
    "/png/front/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.png/extra",
    `${PATH}?version=1`,
    `${PATH}?1&2`,
    `${PATH}?https://evil.example/x.png`,
    "/https://evil.example/x.png",
    "//evil.example/png/front/7/7/77c6fa74-5543-42ac-9ead-0e890b188e99.png",
  ])("404s %s without an upstream fetch", async (path) => {
    const upstream = mockUpstream();
    const response = await get(path);
    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("only answers GET", async () => {
    const upstream = mockUpstream();
    const request = new Request(`https://img.proxy-scaler.com${PATH}`, {
      method: "POST",
      headers: { "User-Agent": UA },
    });
    const response = await worker.fetch(request, env, createExecutionContext());
    expect(response.status).toBe(404);
    expect(upstream).not.toHaveBeenCalled();
  });
});

describe("user agent", () => {
  it.each([null, "", "curl/8.0", "python-requests/2.34", "Proxy-Scaler/1.0", "xproxy-scaler/1"])(
    "403s User-Agent %s without an upstream fetch",
    async (ua) => {
      const upstream = mockUpstream();
      const response = await get(`${PATH}?${VERSION}`, { ua });
      expect(response.status).toBe(403);
      expect(upstream).not.toHaveBeenCalled();
    },
  );
});

describe("storage", () => {
  it("a miss fetches upstream, stores the object and serves it", async () => {
    const upstream = mockUpstream(
      () => new Response("from scryfall", { headers: { "Content-Type": "image/png" } }),
    );
    const response = await get(`${PATH}?${VERSION}`);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("from scryfall");
    expect(response.headers.get("Content-Type")).toBe("image/png");
    expect(upstream).toHaveBeenCalledTimes(1);
    const stored = await env.IMAGES.get(KEY);
    expect(await stored?.text()).toBe("from scryfall");
    expect(Number(stored?.customMetadata?.fetchedAt)).toBeGreaterThan(Date.now() - DAY_MS);
  });

  it("a fresh hit is served from R2 with no upstream fetch", async () => {
    await seed(KEY, "stored copy", 13);
    const upstream = mockUpstream();
    const response = await get(`${PATH}?${VERSION}`);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("stored copy");
    expect(response.headers.get("Content-Type")).toBe("image/png");
    expect(upstream).not.toHaveBeenCalled();
  });

  it("a stale hit re-fetches, overwrites and serves the new copy", async () => {
    await seed(KEY, "old scan", 15);
    const upstream = mockUpstream(() => new Response("new scan"));
    const response = await get(`${PATH}?${VERSION}`);
    expect(await response.text()).toBe("new scan");
    expect(upstream).toHaveBeenCalledTimes(1);
    const stored = await env.IMAGES.get(KEY);
    expect(await stored?.text()).toBe("new scan");
    expect(Number(stored?.customMetadata?.fetchedAt)).toBeGreaterThan(Date.now() - DAY_MS);
  });

  it.each([
    ["an error status", () => new Response("blocked", { status: 429 })],
    [
      "a network error",
      () => {
        throw new Error("connection reset");
      },
    ],
  ])("a stale hit whose re-fetch fails with %s serves the stale copy", async (_, answer) => {
    await seed(KEY, "old scan", 30);
    mockUpstream(answer as Upstream);
    const response = await get(`${PATH}?${VERSION}`);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("old scan");
    expect(await (await env.IMAGES.get(KEY))?.text()).toBe("old scan");
  });

  it("a miss whose fetch fails passes the upstream error through", async () => {
    mockUpstream(() => new Response("no such card", { status: 404 }));
    const response = await get(`${PATH}?${VERSION}`);
    expect(response.status).toBe(404);
    expect(await env.IMAGES.get(KEY)).toBeNull();
  });

  it("a miss whose fetch throws is a 502 and stores nothing", async () => {
    mockUpstream(() => {
      throw new Error("connection refused");
    });
    const response = await get(`${PATH}?${VERSION}`);
    expect(response.status).toBe(502);
    expect(await env.IMAGES.get(KEY)).toBeNull();
  });

  it("different version suffixes are separate cache entries", async () => {
    await seed(KEY, "version 1706239968", 1);
    const upstream = mockUpstream(() => new Response("version 1800000000"));
    const response = await get(`${PATH}?1800000000`);
    expect(await response.text()).toBe("version 1800000000");
    expect(upstream).toHaveBeenCalledTimes(1);
    expect(await (await env.IMAGES.get(KEY))?.text()).toBe("version 1706239968");
    const newKey = KEY.replace(VERSION, "1800000000");
    expect(await (await env.IMAGES.get(newKey))?.text()).toBe("version 1800000000");
  });

  it("respects the CACHE_TTL_DAYS var", async () => {
    await seed(KEY, "two days old", 2);
    const upstream = mockUpstream(() => new Response("refetched"));

    const underDefault = await get(`${PATH}?${VERSION}`);
    expect(await underDefault.text()).toBe("two days old");
    expect(upstream).not.toHaveBeenCalled();

    const underOneDay = await get(`${PATH}?${VERSION}`, {
      envOverrides: { CACHE_TTL_DAYS: "1" } as unknown as Partial<Env>,
    });
    expect(await underOneDay.text()).toBe("refetched");
    expect(upstream).toHaveBeenCalledTimes(1);
  });
});

describe("edge cache", () => {
  it("responses carry a max-age of about a day", async () => {
    mockUpstream();
    const response = await get(`${PATH}?${VERSION}`);
    expect(response.headers.get("Cache-Control")).toBe("public, max-age=86400");
  });

  it("answers from the edge cache before touching R2 or Scryfall", async () => {
    const cached = new Response("edge copy", { headers: { "Content-Type": "image/png" } });
    vi.spyOn(caches.default, "match").mockResolvedValue(cached);
    const upstream = mockUpstream();
    const r2 = vi.spyOn(env.IMAGES, "get");
    const response = await get(`${PATH}?${VERSION}`);
    expect(await response.text()).toBe("edge copy");
    expect(upstream).not.toHaveBeenCalled();
    expect(r2).not.toHaveBeenCalled();
  });

  it("puts fresh responses into the edge cache, but not stale ones", async () => {
    const put = vi.spyOn(caches.default, "put");
    mockUpstream(() => new Response("x", { status: 500 }));
    await seed(KEY, "stale", 30);
    await get(`${PATH}?${VERSION}`);
    expect(put).not.toHaveBeenCalled();

    vi.restoreAllMocks();
    const put2 = vi.spyOn(caches.default, "put");
    mockUpstream();
    await get(`${PATH}?1800000000`);
    expect(put2).toHaveBeenCalledTimes(1);
  });
});
