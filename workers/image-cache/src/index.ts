/**
 * The Proxy Scaler image cache: a read-through cache of Scryfall card
 * images, so each image is fetched from Scryfall at most about once per
 * CACHE_TTL_DAYS no matter how many Proxy Scaler users ask for it.
 *
 * - Only Scryfall card-image paths are accepted, and the upstream URL is
 *   rebuilt from the parsed parts — never taken from the request — so this
 *   can't be used as an open proxy.
 * - User-Agents other than `proxy-scaler/*` get a 403, to keep casual
 *   scrapers from treating the bucket as a free Scryfall mirror. (Per-IP
 *   rate limiting is a WAF rule in the Cloudflare dashboard, not code.)
 * - R2 holds each image with the time it was fetched. A fresh hit is
 *   served as is; a stale one is re-fetched and overwritten, or served
 *   stale if Scryfall can't be reached. A miss is fetched and stored, or
 *   the upstream error is passed through.
 * - The edge cache sits in front with a one-day max-age, so edge copies
 *   never outlive the R2 refresh by much.
 *
 * See docs/adr/0005-image-fetches-via-cloudflare-worker-cache.md.
 */

const UPSTREAM_ORIGIN = "https://cards.scryfall.io";
const CLIENT_UA_PREFIX = "proxy-scaler/";
const UPSTREAM_UA = "proxy-scaler-image-cache/1 (https://proxy-scaler.com)";
const EDGE_MAX_AGE_S = 24 * 60 * 60;
const DAY_MS = 24 * 60 * 60 * 1000;

// /{size}/{face}/{h1}/{h2}/{uuid}.{ext}: Scryfall's own layout, where the
// two directory levels are the uuid's first two hex digits.
const IMAGE_PATH =
  /^\/(small|normal|large|png|art_crop|border_crop)\/(front|back)\/([0-9a-f])\/([0-9a-f])\/(\3\4[0-9a-f]{6}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(jpg|png)$/;
// Scryfall's cache-busting suffix: `?1706239968`, or nothing at all.
const VERSION_QUERY = /^(?:\?(\d{1,12}))?$/;

interface ImageRef {
  /** The path exactly as Scryfall serves it, rebuilt from parsed parts. */
  path: string;
  version: string | null;
}

function parseImageRef(url: URL): ImageRef | null {
  const path = IMAGE_PATH.exec(url.pathname);
  const query = VERSION_QUERY.exec(url.search);
  if (!path || !query) return null;
  const [, size, face, h1, h2, uuid, ext] = path;
  // `png` is the only PNG size; every other size is a JPEG.
  if ((size === "png") !== (ext === "png")) return null;
  return { path: `/${size}/${face}/${h1}/${h2}/${uuid}.${ext}`, version: query[1] ?? null };
}

const withVersion = (ref: ImageRef) => (ref.version ? `${ref.path}?${ref.version}` : ref.path);
/** R2 key: the path plus the version suffix, so a re-scanned image is a new entry. */
const storeKey = (ref: ImageRef) => withVersion(ref).slice(1);

function ttlMs(env: Env): number {
  const days = Number(env.CACHE_TTL_DAYS);
  return (Number.isFinite(days) && days > 0 ? days : 14) * DAY_MS;
}

function imageResponse(
  body: BodyInit,
  contentType: string | undefined,
  cacheStatus: "HIT" | "MISS" | "REFRESH" | "STALE",
): Response {
  return new Response(body, {
    headers: {
      "Content-Type": contentType ?? "application/octet-stream",
      "Cache-Control": `public, max-age=${EDGE_MAX_AGE_S}`,
      "X-Image-Cache": cacheStatus,
    },
  });
}

/** Fetch from Scryfall. Never throws: a network error becomes a 502. */
async function fetchUpstream(ref: ImageRef): Promise<Response> {
  try {
    return await fetch(`${UPSTREAM_ORIGIN}${withVersion(ref)}`, {
      headers: { "User-Agent": UPSTREAM_UA },
    });
  } catch (err) {
    return new Response(`Upstream fetch failed: ${String(err)}`, { status: 502 });
  }
}

async function serveFromStore(ref: ImageRef, env: Env): Promise<Response> {
  const key = storeKey(ref);
  const stored = await env.IMAGES.get(key);
  if (stored) {
    const fetchedAt = Number(stored.customMetadata?.fetchedAt ?? 0);
    if (Date.now() - fetchedAt < ttlMs(env)) {
      return imageResponse(stored.body, stored.httpMetadata?.contentType, "HIT");
    }
  }

  const upstream = await fetchUpstream(ref);
  if (!upstream.ok) {
    if (stored) {
      // Scryfall is blocking us or down: the old copy beats no image.
      return imageResponse(stored.body, stored.httpMetadata?.contentType, "STALE");
    }
    return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText });
  }
  // The stored body is never read on this path; release it.
  await stored?.body.cancel();

  const bytes = await upstream.arrayBuffer();
  const contentType = upstream.headers.get("Content-Type") ?? undefined;
  await env.IMAGES.put(key, bytes, {
    httpMetadata: contentType ? { contentType } : undefined,
    customMetadata: { fetchedAt: String(Date.now()) },
  });
  return imageResponse(bytes, contentType, stored ? "REFRESH" : "MISS");
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const ua = request.headers.get("User-Agent") ?? "";
    if (!ua.startsWith(CLIENT_UA_PREFIX)) {
      return new Response("Forbidden", { status: 403 });
    }
    const url = new URL(request.url);
    const ref = request.method === "GET" ? parseImageRef(url) : null;
    if (!ref) {
      return new Response("Not found", { status: 404 });
    }

    // Keyed on the rebuilt URL, so headers and query spelling can't split
    // or poison the edge cache.
    const edgeKey = new Request(`${url.origin}${withVersion(ref)}`);
    const edge = await caches.default.match(edgeKey);
    if (edge) return edge;

    const response = await serveFromStore(ref, env);
    // Stale copies stay out of the edge cache so the next request retries
    // Scryfall rather than pinning the stale copy for another day.
    const status = response.headers.get("X-Image-Cache");
    if (response.ok && status !== "STALE") {
      ctx.waitUntil(caches.default.put(edgeKey, response.clone()));
    }
    return response;
  },
} satisfies ExportedHandler<Env>;
