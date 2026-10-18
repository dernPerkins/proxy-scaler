# Proxy Scaler MPC Fill catalogue crawler

A Cloudflare Workflow that builds a searchable catalogue of the MPC Fill
uploaders' public Google Drive folders once a day. It publishes the
catalogue to R2 as one gzipped NDJSON shard per source plus a
`manifest.json`, served on `catalog.proxy-scaler.com`. Each generation
server downloads only the shards that changed into its local SQLite
(`proxy_scaler/mpc_catalog.py`), so searches never reach mpcfill.com.

- Output format, which servers test against:
  [`.scratch/mpcfill-art-picker/phase-c/format.md`](../../.scratch/mpcfill-art-picker/phase-c/format.md).
- Why it works this way:
  [ADR-0006](../../docs/adr/0006-mpc-fill-art-via-generation-server.md) and
  [ADR-0007](../../docs/adr/0007-mpc-art-catalog-on-cloudflare-workflows.md).

## What it does

Each run is one Workflow instance with these steps:

1. **`sources`** makes one `GET https://mpcfill.com/2/sources/`, which is
   the only MPC Fill request of the day. It keeps sources whose
   `externalLink` is a Drive folder link, in MPC Fill's order. Sources
   without a link are skipped; their folder ids are hidden on purpose and
   are never recovered.
2. **`crawl:<key>:<n>`** steps walk each source's folder tree
   breadth-first with Drive v3 `files.list` (`pageSize=1000`, our API
   key).
   - Requests run one at a time with a short gap. Each request times out
     after 30 seconds, and backs off exponentially on timeouts, network
     errors, 429, 5xx and Drive's rate-limit 403s.
   - A big source is split into chunks: one step per `CRAWL_CHUNK_FOLDERS`
     (300) folder listings, or 10 minutes, whichever comes first. Between
     chunks, the BFS queue and the records found so far are kept in R2
     under `tmp/<instanceId>/<key>/`. A failed chunk is retried on its
     own; finished chunks aren't re-listed.
   - Names are parsed by `src/parse.ts`. Discard-pile folders (`!Removed`,
     `Trash`, `Old Versions`; `Old School` is kept) are skipped and
     logged.
   - Images that aren't card-shaped (portrait 0.62–0.80 or landscape
     1.25–1.60 w/h, at 200 dpi or more) are skipped and counted as
     `skipped_shape`. This drops MTG Arena texture dumps and set symbols.
   - The chunk that empties the queue writes all of the source's records
     as one gzipped shard at `sources/<key>.<sha256>.ndjson.gz`.

   Unchanged Drive contents give byte-identical shards, however the crawl
   was chunked, so servers skip them. If a source still fails after
   retries (a 404 or private folder fails at once), it keeps its previous
   manifest entry.
3. **`publish`** deletes the run's `tmp/` objects (and any older than two
   days left by runs that never got this far). It then writes
   `manifest.json` and deletes every shard that neither the new nor the
   previous manifest refers to. A server holding yesterday's manifest can
   still download its shards.

Only metadata is listed. Card images never pass through this Worker or the
bucket: each generation server fetches them from Google itself.

The parsing rules come from folder and file names observed in real source
folders (see the comment at the top of `src/parse.ts`). They were **not**
taken from mpc-autofill or CriProx, which are both GPL. Keep it that way
when changing them. Rules worth checking against real data are listed
under "Open questions" in format.md.

## Developing

```bash
cd workers/mpc-catalog
npm install
npm test          # vitest in the Workers runtime: in-memory R2, mocked MPC Fill and Drive
npm run typecheck
```

Run `npx wrangler types --env-interface Env` after changing
`wrangler.jsonc`, since it regenerates `worker-configuration.d.ts`.

The tests never use a real key or touch the network.
- `test/parse.test.ts` is table tests for the naming rules.
- `test/workflow.test.ts` runs whole Workflow instances against a fake
  mpcfill.com and Drive, with two folders per chunk. It covers:
  - pagination, nested folders and a skipped discard folder;
  - a failing source keeping its old shard;
  - the manifest's sha256 over the stored gzip bytes, and shard garbage
    collection;
  - a source spanning several chunks;
  - a failed chunk resuming without re-listing finished folders;
  - a hung request timing out.
- `remoteBindings: false` in `vitest.config.ts` keeps tests away from the
  real bucket, even though `wrangler.jsonc` has a remote binding.
- The `uncaught exception` lines in the test output are the Workflow
  engine logging the deliberately failing sources; they aren't failures.

## First-time setup

Dean does all of this by hand; nothing here is automated.

### 1. A Google API key for the Drive API

1. In the [Google Cloud console](https://console.cloud.google.com/), create
   a project, for example `proxy-scaler-catalog`.
2. **APIs & Services → Library**: find **Google Drive API** and click
   **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key**.
4. Edit the key:
   - **API restrictions → Restrict key → Google Drive API** only.
   - Application restrictions: leave **None**. Workers call from
     Cloudflare's shared IPs, so an IP restriction can't be used.
5. Copy the key. It only reads public metadata, but keep it out of the
   repo anyway.

The default Drive quota (requests per minute per project) is far above
what one serial crawler uses, and the API key path is free.

### 2. Store the key as a Worker secret

```bash
cd workers/mpc-catalog
npx wrangler secret put GOOGLE_API_KEY
```

Paste the key at the prompt. It lives only in Cloudflare and is never
written to `wrangler.jsonc`.

### 3. The bucket and its public domain

```bash
npx wrangler r2 bucket create proxy-scaler-mpc-catalog
```

Then, in the Cloudflare dashboard:

1. **R2 → proxy-scaler-mpc-catalog → Settings → Custom Domains → Add**:
   `catalog.proxy-scaler.com`. This is the bucket's own public domain;
   the Worker serves nothing. Leave the `r2.dev` URL disabled.
2. **proxy-scaler.com → Caching → Cache Rules → Create rule**:
   - *Shards are immutable*: when Hostname equals
     `catalog.proxy-scaler.com` **and** URI Path starts with `/sources/`,
     then **Eligible for cache**:
     - Edge TTL: *Ignore cache-control header*, 1 year;
     - Browser TTL: *Override origin*, 1 year.

     Shard paths are content-addressed, so a path's bytes never change.
     The objects also carry `Cache-Control: public, max-age=31536000,
     immutable`.
   - Optionally, a second rule for URI Path equals `/manifest.json`:
     Edge TTL 5 minutes. `manifest.json` changes once a day and carries
     `max-age=300`.

Don't add a Transform Rule or compression setting that sets
`Content-Encoding` on `/sources/*`. Servers hash the stored gzip bytes
exactly as served.

### 4. Deploy

```bash
cd workers/mpc-catalog
npm test
npm run deploy    # wrangler deploy
```

The deploy registers the Workflow `proxy-scaler-mpc-catalog-crawl` with
its schedule (`0 6 * * *`, 06:00 UTC daily). Deploys are manual; nothing in
CI touches this Worker.

If the deploy rejects `limits.subrequests` or `limits.cpu_ms` in
`wrangler.jsonc`, lower them to what the account's plan allows.
- `subrequests` needs to cover one chunk: a request per Drive folder
  page for up to `CRAWL_CHUNK_FOLDERS` folders, plus retries.
- Each source takes one step per chunk, so a run has a few hundred
  steps. If a run ever fails on the per-instance step limit, add
  `"limits": {"steps": …}` to the Workflow in `wrangler.jsonc`.
- `cpu_ms` only counts parsing, gzip and hashing, not waiting on Drive.

## Running by hand

Start with a dry run of one or two sources. It writes under
`DRY_RUN_PREFIX` (`dev/`) and leaves the live manifest alone:

```bash
npx wrangler workflows trigger proxy-scaler-mpc-catalog-crawl '{"dryRun": true, "only": ["JohnPrime", "WarpDandy"]}'
```

Parameters (all optional; the daily scheduled run passes none):

| param | effect |
|---|---|
| `dryRun` | write `dev/manifest.json` and `dev/sources/…` instead of the bucket root |
| `only` | crawl just these source keys. Everything else keeps its previous entry (or is left out if it has none). |

A full live run by hand is the same command without parameters:

```bash
npx wrangler workflows trigger proxy-scaler-mpc-catalog-crawl
```

Avoid starting a second run while one is still going. Each run's `publish`
garbage-collects shards that its own two manifests don't refer to, which
can include shards the other run hasn't published yet.

### Watching a run and checking the result

```bash
npx wrangler workflows instances list proxy-scaler-mpc-catalog-crawl
npx wrangler workflows instances describe proxy-scaler-mpc-catalog-crawl latest
```

`describe` shows each step's status and output.
- An unfinished chunk `crawl:<key>:<n>` gives its progress so far:
  `folders`, `queued`, `requests` and `images`.
- A source's last chunk gives its summary: `count`, `folders`,
  `requests`, `chunks`, and `skipped` (the discard folders it left out,
  to check against the rule).

`wrangler tail` prints a progress line every 50 folders, such as
`WarpDandy: 150 folders done, 412 queued, 163 requests, 9120 images`.
A run that has gone quiet for longer than a chunk's 30-minute step
timeout is stuck.

The instance's output lists the sources that `failed`, the ones `kept` from
the previous manifest, and how many old shards were `deleted`. Live logs:
`npx wrangler tail proxy-scaler-mpc-catalog`.

Then look at the manifest:

```bash
curl -s https://catalog.proxy-scaler.com/dev/manifest.json | jq '.version, (.sources | length), .sources[0]'
curl -s https://catalog.proxy-scaler.com/manifest.json | jq '.version, ([.sources[].count] | add)'
```

Or read it straight from R2:

```bash
npx wrangler r2 object get proxy-scaler-mpc-catalog/manifest.json --remote --pipe | jq .
```

To look inside a shard:

```bash
curl -s https://catalog.proxy-scaler.com/sources/<file> | gunzip | head
```

A shard's `sha256` in the manifest is over the gzipped bytes, so
`curl -s …/sources/<file> | shasum -a 256` must match it.

## Tuning

These `vars` live in `wrangler.jsonc`. After editing, run
`npx wrangler types --env-interface Env` and `npm run deploy`.

- `DRIVE_GAP_MS` (100): the pause between Drive requests, which run one
  at a time.
- `DRIVE_BACKOFF_MS` (2000): the first retry delay after a 429 or 5xx,
  doubled each time with six attempts in all. `Retry-After` is honoured.
  After that the step itself retries twice, minutes apart.
- `DRIVE_TIMEOUT_MS` (30000): how long a Drive request may go unanswered
  before it is aborted and retried.
- `CRAWL_CHUNK_FOLDERS` (300): folder listings per crawl step.
- `DRY_RUN_PREFIX` (`dev/`): where dry runs write.

The User-Agent is `proxy-scaler-catalog/<version> (+https://proxy-scaler.com)`.
The version is `USER_AGENT` in `src/http.ts`; bump it with `package.json`.
