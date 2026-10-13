# Proxy Scaler image cache

A Cloudflare Worker on `img.proxy-scaler.com` that caches Scryfall card
images in R2. The generation server fetches card images through it by
default and falls back to Scryfall directly whenever it fails, so an outage
here costs latency, never correctness. Users can turn it off in App
Settings, and the CLI has `--direct-scryfall`. The Worker address is a
constant in `proxy_scaler/scryfall.py` (`WORKER_IMAGE_HOST`), not a setting.

Why it exists and how it's protected:
[ADR-0005](../../docs/adr/0005-image-fetches-via-cloudflare-worker-cache.md).

## What it does

- Accepts only `GET /{small|normal|large|png|art_crop|border_crop}/{front|back}/{h}/{h}/{uuid}.{jpg|png}`
  with an optional numeric version query (`?1706239968`), which is Scryfall's
  own layout. Anything else is a 404. The upstream URL on `cards.scryfall.io`
  is rebuilt from the parsed parts.
- Requests whose User-Agent doesn't start with `proxy-scaler/` get a 403.
- R2 key = path + version suffix. Each object records when it was fetched:
  - **fresh hit** (younger than `CACHE_TTL_DAYS`): served from R2;
  - **stale hit:** re-fetched from Scryfall and overwritten, or served stale
    if the re-fetch fails;
  - **miss:** fetched, stored and served, or the upstream error is passed
    through (a network error becomes a 502).
- The edge cache sits in front with `max-age=86400`. Stale copies are never
  put in it. The `X-Image-Cache` response header says which path served a
  request: `HIT`, `MISS`, `REFRESH` or `STALE`.
- Per-IP rate limiting is a WAF rate-limiting rule in the dashboard, not
  Worker code. On the Free plan the rule can't match a hostname, so it
  matches the cache's path shape: URI Path wildcard `/*/front/*` or
  `/*/back/*`, per IP, 100 requests per 10 seconds (Free allows only
  10-second periods; a fresh picker grid fires ~50 thumbnail requests at
  once from one generation server, so 50 would block normal use). A
  tripped rule only sends that client to Scryfall directly.

## Developing

```bash
cd workers/image-cache
npm install
npm test          # vitest in the Workers runtime: in-memory R2, mocked upstream
npm run typecheck
```

Run `npx wrangler types` after changing `wrangler.jsonc`, since it
regenerates `worker-configuration.d.ts`.

## First-time setup

The R2 bucket, the custom domain, the WAF rule and the first deploy are
one-off steps in the Cloudflare account. `./setup-wizard.sh` walks through
all of them and checks the result at the end.

## Deploying a change

```bash
cd workers/image-cache
npm test
npm run deploy    # wrangler deploy
```

Deploys are manual; nothing in CI touches the Worker.

### Changing the TTL

`CACHE_TTL_DAYS` in `wrangler.jsonc` (`vars`) is how many days a stored image
is trusted before the next request re-fetches it (14 to start). Edit it,
run `npx wrangler types`, then `npm run deploy`. Objects already in R2
pick up the new TTL on their next request, since freshness is checked
against the stored fetch time on every request.

## Rollout order

Deploy the Worker before shipping a client release that defaults to it.
Until it's live, every image request fails over to Scryfall directly,
which is correct but slower.
