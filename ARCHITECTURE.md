# Architecture

Living document — re-read this before touching anything architecture-level,
and update it in the same change that shifts the architecture. Out of date
docs are worse than no docs; keep this current or delete the section.

This is the implementation-level reference: diagrams, endpoint/command
inventories, and data-flow detail. Hard-to-reverse architectural
decisions and their rationale live in [`docs/adr/`](./docs/adr/).

## Status

**Target architecture below has landed in code, unverified by a real
build.** The backend reshape, `project_store.rs`, and the frontend
rewiring are all done and match this document. Two things remain purely
because of sandbox limits, not design gaps: no Rust toolchain was
available to actually `cargo build`/`cargo tauri dev` this (read
project_store.rs carefully, or run a build, before trusting it blind),
and the manual end-to-end pass described under Verification in the plan
(offline project creation, resolve/generate against a real server,
Local↔Remote mid-session switch) hasn't been run for real. Check `git
log` if this document and the code ever disagree — the code wins.

## The core split

See [ADR-0001](./docs/adr/0001-split-project-and-generation-concerns.md)
for why the split happened and what it costs to reverse. The terms that
matter throughout this document:

- **Project** — a decklist workspace: its raw text, unresolved card list,
  and per-project preferences (model, DPI targets, tile size, PDF
  layout). Always lives on the machine running the desktop client, never
  on a generation server. A name is optional — see **Unnamed Project**.
- **Unnamed Project** — an ordinary `projects` row whose `name` is `''`.
  One concept, two states, not a second noun: it has a real id, a real
  `tag`, and its own persisted cards and settings. At most one exists at
  a time (`name TEXT NOT NULL UNIQUE` does the enforcing), and it is
  hidden from the picker by `WHERE name <> ''`. Naming it *is* saving it.
  See [ADR-0002](./docs/adr/0002-projects-are-optional-and-promotion-preserves-project-tag.md).
- **Generation server** — the Python FastAPI + worker pair that resolves
  cards against Scryfall, downloads, upscales, and assembles PDFs. Has no
  concept of a "project."
- **Card corpus** — the generation server's locally-imported copy of
  Scryfall's bulk card data (`scryfall_cards.db`, owned by
  `proxy_scaler/carddb.py`), imported on demand from the sidebar's Card
  database panel. Powers the change-printing picker (variants share an
  `oracle_id`) and makes resolution local-first. Per-server, disposable,
  and optional — every reader tolerates its absence. See
  [ADR-0003](./docs/adr/0003-local-scryfall-corpus-in-separate-sqlite-file.md).
- **`project_tag`** — an opaque string minted when a Project row is
  created, attached to every generation request, used purely to
  scope/filter that project's tasks and images. Deliberately *not* a
  foreign key; the generation side has no projects table to point at.
  One tag per row, for that row's whole life: **naming an Unnamed
  Project is an `UPDATE` that never touches `tag`**, so images generated
  before the name stay attached. A tag ends when its row is deleted —
  by `delete_project` or `clear_all_projects`, named or not, and by New
  or discard from an Unnamed Project — and the next write path to need a
  row (a decklist import *or* a settings change) lazily creates one with
  a fresh tag. So a tag is minted per row, not once per named project:
  over a session a user can mint, discard, and mint again several times.

`generation_tasks.project_id` was already nullable and never joined
before the split — the split just makes that looseness the *only*
relationship, instead of an accident of a shared database.

## Process diagram

```
┌─────────────────────────────────────────────────────────┐
│ Tauri desktop app (single process)                       │
│                                                            │
│  ┌──────────────────────┐        ┌─────────────────────┐ │
│  │ Rust: project store    │        │ React UI             │ │
│  │ (project_store.rs)     │◄──────►│                       │ │
│  │                        │ invoke │ Project CRUD/decklist │ │
│  │ rusqlite, own SQLite   │        │ editing → invoke()    │ │
│  │ file in app data dir.  │        │                       │ │
│  │ No network calls.      │        │ Generation/status/    │ │
│  │ No separate process.   │        │ images/PDF → fetch()  │ │
│  └──────────────────────┘        └───────────┬──────────┘ │
└──────────────────────────────────────────────┼────────────┘
                                                 │ HTTP, project_tag-scoped
                                                 ▼
                          ┌───────────────────────────────────────┐
                          │ Generation server (Local sidecar OR    │
                          │ Remote — user's Local/Remote toggle)   │
                          │                                        │
                          │ Python FastAPI + worker (unchanged     │
                          │ supervisor.py lifecycle). Owns:        │
                          │  - Scryfall resolution (local card     │
                          │    corpus first, live API fallback)    │
                          │  - the card corpus (scryfall_cards.db, │
                          │    bulk-imported on demand)            │
                          │  - download + upscale pipeline         │
                          │  - generation_tasks queue               │
                          │  - generated_images registry +         │
                          │    project_gallery_memberships         │
                          │  - PDF assembly                        │
                          │ No concept of "projects."               │
                          └───────────────────────────────────────┘
```

The generation server itself can be: the embedded sidecar this app
spawns (`desktop/src-tauri/src/main.rs::start_local_server`), a
standalone status-window app on another Windows/macOS machine
(`desktop/server-app/`), or a headless Linux service installed from the
`.deb` (`packaging/`). All three run the exact same
`proxy_scaler.supervisor`/`proxy_scaler.api` code — only how it's
launched and reached differs. See `desktop/README.md` and root
`README.md`'s "Server" section for the operational side of each.

## Why this split doesn't require rewriting core logic

Recorded here because it's not obvious from the code alone, and it's the
reason this migration is a reshape rather than a rewrite:

- **No SQL join ever spanned project data and generation data**, even
  before the split. The only real joins in the old combined `db.py` were
  project-internal (`project_gallery_items`↔`project_cards`).
  `generation_tasks` was always queried standalone by a plain
  `project_id` filter.
- **`generation_tasks` rows were already fully denormalized at enqueue
  time** — the worker never needed a live lookup into project data to do
  its job. Every field it needs (`scryfall_id`, face info, `png_url`,
  `dpi`, `model`, output paths) is already on the row.
- **The identity-matching/status-merge logic** (`card_identity`,
  `build_rows`, `group_by_card`, `status_for_pairs` — formerly
  `proxy_scaler/services/decklist.py`, now `desktop/frontend/src/mergeCardStatus.ts`)
  already operated on two independently-fetched in-memory lists, merged
  via plain string keys built from `scryfall_id`/`set_code`/
  `collector_number`/`face_index`/`dpi`/`model` — never a database join.
  It ported to TypeScript unchanged in shape.
- **The gallery table already redundantly stored every identity field**
  on each row. Its only real dependency on `project_cards` was the
  `card_id` foreign key, which became a plain `project_tag TEXT` column
  — a small, mechanical schema change. (Migration 6 later reshaped that
  table again into the global `generated_images` registry — one row per
  physically distinct image, `scryfall_id` always real — plus a
  `project_gallery_memberships` relation saying which projects show
  which images. The registry is the authoritative "does this image
  exist" answer: query paths never stat the filesystem; only the
  explicit adopt/prune reconcile does. See
  `docs/adr/0004-generated-image-registry.md`.)

This transition is now formally tracked as migration 1 in `db.py`'s
versioned schema-migration system (`_MIGRATIONS`, applied via
`PRAGMA user_version`) rather than untracked ad hoc shape-detection —
see the module docstring in `proxy_scaler/db.py` for how later schema
changes are meant to build on it non-destructively.

## The resolve → generate flow

Project management makes zero external network calls — all Scryfall
traffic lives on the generation side, collapsed into one step instead of
the old architecture's two separate resolutions (once at decklist import,
again at generate time):

1. **Import is resolve-gated**: the Import Cards button is disabled while
   the generation server is unreachable, and only successfully matched
   cards enter the list. The frontend parses the pasted text via the Rust
   `parse_decklist` command (a direct Rust port of the old
   `proxy_scaler/decklist.py` — quantity + name + optional set/collector,
   or a set-less trailing collector number like `1 Sol Ring 263`, kept as
   a hint since some deck managers can't export more for non-English
   cards), resolves the entries through `POST /api/resolve`, and persists
   the successes — fully pinned rows (`scryfall_id`, canonical name,
   `printed_name` for non-English printings, set/collector/lang) — via
   `import_resolved_cards` in one transaction. Failed lines are listed
   with their errors, stay in the textarea for fixing, and are **not**
   added. If there is no project yet, the first successful import creates
   the Unnamed Project. `name`/`set_code`/`collector_number`/`lang`/
   `printed_name` stay denormalized on the row so decklists render fully
   offline; rows show `printed_name` when set, with the English name as
   the tooltip.
2. **Language modes** (the controls beside the Import Cards button): the
   language dropdown (full Scryfall list from `GET /api/cards/languages`,
   default `en`) is **strictly literal** — the resolved object's `lang`
   must equal the selection (`ResolveIn.strict_lang`), else that line
   errors. The "All Languages" checkbox disables the dropdown and matches
   best-effort across languages (a German-typed name fuzzy-matches its
   German printing; an English line lands on the English one). Cards
   imported before this flow existed are re-resolved on project load,
   best-effort and non-strict, purely to backfill their pins.
3. Server-side resolution is **local-first** (`card_lookup.CardResolver`,
   used by both `/api/resolve` and `/api/generate`): pinned `scryfall_id`
   → set+collector in the entry's preferred language (English fallback,
   or exactly the demanded language under strict_lang)
   → name + collector-number hint for set-less lines (matched against the
   name's printings — English or printed names — in any set; a hint that
   matches nothing degrades to the name-only path with a warning) →
   exact name (English or printed), all against the card corpus; only
   misses (cards newer than the last import, fuzzy names) and image
   downloads touch the live Scryfall API — where Scryfall's fuzzy lookup
   also matches localized printed names and returns the foreign printing.
   No corpus imported → everything falls through live, exactly the
   pre-corpus behavior (a set-less hint resolves as name-only there, with
   a warning that the number went unused).
4. **Changing a printing**: the card row's set/collector button
   (`PrintingPicker.tsx`) fetches `GET /api/cards/variants` — every
   printing sharing the card's `oracle_id`, from the corpus, newest
   first, filterable by language — and a pick writes the new
   `scryfall_id` + display fields through `set_card_printing`. The card's
   identity string changes, so the adopt effect re-buckets its gallery
   badges automatically.
5. On **Generate**, the frontend sends the project's card list (each
   entry carrying its pinned `scryfall_id` + `lang`) plus the project's
   `project_tag` to `POST /api/generate`. The generation server resolves
   (local-first), downloads, and upscales each face as one pipeline per
   task — not three separate round trips.
6. Status polling (`GET /api/tasks?project_tag=...`) and generated images
   (`GET /api/gallery/{id}/...`) are scoped by that same tag. The
   frontend merges this generation-side data with its own local project
   card list client-side, using `mergeCardStatus.ts`.

Printing language is part of identity end-to-end — an exact printing is
`set/collector/lang` (absent lang normalizes to `en`) everywhere identity
is compared: import dedup (project_store.rs), status-badge bucketing
(mergeCardStatus.ts), gallery adoption and scan matching (db.py), and PDF
quantity matching (pipeline.face_group_key / pdf_layout.match_quantities)
— so the Italian and English Sol Ring of one set/collector are two cards
with independent images. Additionally, `lang` rides on tasks
and gallery rows (db migration 5), and non-English printings carry a lang
segment in output filenames (`Name-SET-COLLECTOR-ja-...png`) so two
languages of one printing never collide on disk. English filenames keep
the exact pre-language shape on purpose — nothing regenerates on upgrade.

## Endpoint / command inventory

### Rust (Tauri commands — `desktop/src-tauri/src/project_store.rs`)

All project management. Local-only, no network, callable regardless of
generation-server state.

| Command | Purpose |
|---|---|
| `get_or_create_unnamed_project` | The Unnamed Project's summary (id + tag), creating the row on first call. Called from the write paths — never from `open_db`, which runs on every command and must not write |
| `create_project` | New **named** project (name, empty decklist, default settings). Still rejects an empty name: it always INSERTs, so an Unnamed Project reaching it would mint a second tag |
| `discard_unnamed_project` | Delete the Unnamed Project row if one exists, returning its tag (never creating one). What **New from a named Project** uses: detaching alone isn't a blank slate, since `get_or_create` would otherwise hand a pre-existing Unnamed row — cards, tag and all — to the "new" project at its first write |
| `list_projects` | Summaries for the project picker. `WHERE name <> ''` — the Unnamed Project is deliberately absent. Any future query that lists or counts projects needs the same clause; nothing in the schema enforces it |
| `get_project` | Full record: name, decklist text, unresolved card list, settings. By id, so the Unnamed Project loads like any other |
| `update_project` | Write name + settings. Also the **promotion** path: naming an Unnamed Project is an `UPDATE ... SET name = ?` that never touches `tag`. Accepts `''` (the row must be writable before it is named) but never *un-names* — a blank name leaves the stored one alone, and the UNIQUE constraint is the collision check |
| `delete_project` | Delete one project (cascades to its cards). Also the whole of "discard": deleting the Unnamed Project row is what New/discard does |
| `clear_all_projects` | Delete everything, confirm-gated. Bare `DELETE FROM projects` — takes the Unnamed Project with it |
| `import_decklist_text` | Legacy insert-first import (parsed, unresolved rows). The UI now uses the resolve-gated pair below; this stays for scripted/test use |
| `parse_decklist` | Parse only, no DB writes — the first half of the resolve-gated import |
| `import_resolved_cards` | The second half: insert already-resolved (fully pinned) cards in one transaction, de-duped at every identity tier (set/collector, name+collector, English and printed names); also mirrors the pasted text onto the row |
| `remove_card` | Drop one parsed line from a project |
| `set_card_quantity` | Set a card's copy count (clamped to ≥ 1 — removal is `remove_card`'s job) |
| `set_card_printing` | Change one card to a different printing: pins `scryfall_id` and refreshes the display cache (name/set/collector/lang). The user-chosen twin of a resolution |
| `set_cards_resolution` | Batched persist of post-import resolve results — one transaction for the whole decklist (see the resolve → generate flow, step 2) |
| `get_last_project_id` / `set_last_project_id` | Auto-load-on-launch support (`app_settings`) |
| `get_quit_prompt_suppressed` / `set_quit_prompt_suppressed` | The quit prompt's "Don't ask again" (`app_settings`, alongside `last_project_id` and `recent_remote_hosts`). Absent means "still offer" |
| `list_recent_hosts` / `add_recent_host` / `remove_recent_host` | Remembered Remote address+port pairs for the connection screens (`app_settings`) |
| `get_update_skipped_version` / `set_update_skipped_version` | The update prompt's "Skip this version" (`app_settings`) — suppresses exactly one release; the next one supersedes the skip by not matching it |
| `get_update_check_enabled` / `set_update_check_enabled` | The Decklist sidebar's "Check for updates at launch" (`app_settings`, default on) — the boot check is an unauthenticated request to the release host, so it gets an off switch |

The update mechanism itself lives in `desktop/src-tauri/src/update.rs`
(`check_for_update` / `download_update` / `launch_installer`, driven by
`components/UpdatePrompt.tsx` on boot): the manifest at
`https://dl.proxy-scaler.com/latest.json` (built and minisign-signed by
`packaging/generate-manifest.py`, see `docs/releasing.md`) is fetched in
Rust — CORS-exempt, so the host needs no CORS setup — **verified against
the public key compiled into the app** (the manifest names each
installer's URL *and* sha256, so the manifest is the trust anchor; a bad
or missing signature reads as "no update"), compared against
`CARGO_PKG_VERSION`, and matched to this build via platform + arch + the
`gpu-variant` marker the Makefile bakes into the frozen sidecar. The
chosen artifact's URL/destination/hash stay in Rust-managed state
(`PendingUpdate`) — the webview triggers `download_update` and
`launch_installer` but never supplies their parameters. The download
streams to the Downloads directory (https-only, size-capped, hashed off
the wire); `launch_installer` re-verifies size + sha256 before handing
the file to the OS installer and exiting. Linux (tarball/.deb) and dev
builds get a download-page link instead.

Settings reach SQLite by **write-through on change**, not by an explicit
Save — the shipped UI has no Save button. That covers the Unnamed
Project and named Projects alike, and it is what makes "nothing is
unsaved" literally true. The quit prompt (`components/QuitPrompt.tsx`,
driven by `main.rs`'s `CloseRequested` handler) therefore offers naming,
not saving; **macOS Cmd+Q and menu Quit fire no close event at all and
skip it deliberately** — see ADR-0002. Its two transport commands
(`quit_prompt_listening` / `answer_quit_prompt`) live in `main.rs`, not
here: they carry the modal's answer back to the close handler and touch
no project data.

Project settings that live here: `model`, `dpi_targets`, `skip_existing`,
`tile_size` — genuine per-project preferences. Deliberately **not**
here: `output_dir`/`cache_dir`/`weights_dir`. Those are filesystem paths
meaningful only on whichever machine is generating — a path valid on
your Mac is meaningless against a Remote Linux host — and they aren't
user-configurable at all: the client always sends the fixed relative
names in `DecklistPage.tsx`'s `DEFAULT_GEN_PATHS` (`output` /
`imgcache` / `weights`), the server resolves them against its own cwd
and reports the result via `GET /api/paths`, and the sidebar shows them
read-only with an open-in-file-manager action (local server, `main.rs`'s
`open_directory`) or a terminal window ssh'd into the server and cd'd to
the directory (remote server, `main.rs`'s `open_remote_terminal` —
deliberately not an `sftp://` URL handed to the OS, whose scheme handler
is a lottery: VLC commonly claims it on Linux).

**Back Library** (`desktop/src-tauri/src/back_images.rs`):
`list_back_images`, `add_back_image`, `set_back_image_label`,
`set_back_image_includes_bleed`, `count_projects_using_back_image`,
`delete_back_image`, `back_image_thumbnail`, `get_default_back_image_id`,
`set_default_back_image_id`, `sync_back_image`. App-global rather than
per-project; a project holds a nullable `back_image_id` pointing into it,
copied once from the app default at creation and never live-followed. See
[ADR-0003](./docs/adr/0003-back-images-are-client-owned-and-never-upscaled.md).

### HTTP (generation server — `proxy_scaler/api/`)

No "projects" router. Every request is scoped by an opaque `project_tag`
string, not a database relationship.

| Method + path | Purpose |
|---|---|
| `POST /api/resolve` | Resolve raw entries (card corpus first, live Scryfall fallback); no writes on the generation side. The client persists the returned `scryfall_id`s into its own `project_cards` (resolve → generate flow, step 2) |
| `POST /api/generate` | Resolve (if needed) + enqueue download+upscale per face |
| `GET /api/tasks?project_tag=` | List tasks for a project |
| `GET /api/tasks/{id}` | Single task status |
| `POST /api/tasks/{id}/cancel` | Cancel a queued/running task |
| `GET /api/worker/status` | Is the background worker alive, and is it held (see `/api/worker/release`) |
| `POST /api/worker/release` | Release a worker started held. The desktop app spawns its embedded server with `--hold-worker` so tasks left over from the last session don't start processing at launch; the client asks the user to resume or cancel them (ResumeTasksPrompt), then releases. `POST /api/tasks/cancel-all?include_running=true` is allowed only while held — a held worker has claimed nothing, so `running` rows are provable orphans. Headless/standalone/remote servers never hold |
| `GET /api/gallery?project_tag=` | List generated images for a project |
| `POST /api/gallery/adopt` | Register already-existing images for matching cards into this `project_tag`'s gallery: other projects' rows plus an `output_dir` filename scan for row-less files — called on import/load so already-generated cards show without a Generate request |
| `GET /api/gallery/{id}/original` \| `/full` | Serve image bytes |
| `POST /api/pdf` | Assemble a PDF from `project_tag` + layout + card list **with quantities** (quantity is project data the generation server has no other way to know) |
| `POST /api/export/zip` | Stream a ZIP of the project's generated images, built from the same matching pipeline as the PDF. Two formats: `default` (each unique matched face once under `FRONT/`, plus the Selected Back — if synced — as the single `BACK/` entry) and `tcgplaytest` (the vendor's paired layout: one `FRONT/NNN` + `BACK/NNN` file pair per physical copy, quantities expanded, counts equal). Files are copied byte-for-byte — no resize, no bleed. Synchronous (no job/polling): zipping is disk-speed file copying, unlike the PDF's per-image render |
| `POST /api/export/zip/preview` | The numbers behind the Export tab: both formats' file counts, `missing` / `missing_at_dpi` (same semantics as the PDF preview), and how many tcgplaytest backs would need the Selected Back |
| `GET /api/models` | Enumerate upscale models (static, unchanged) |
| `GET /api/paths` | Absolute resolved output/cache/weights dirs on the generation machine (fixed relative names resolved against the server's cwd) |
| `POST /api/generated-data/clear` | Wipe output/cache dirs on the generation machine. Back Images are **not** touched: they live in `backs/`, a sibling of both, so the exemption is a property of where the files are rather than a condition anyone has to remember |
| `POST /api/tags/{project_tag}/discard` | Forget a thrown-away session (Back Images are exempt — the library is app-global, so a discarded tag has no claim on them): cancel that tag's pending tasks + drop its generation records. Deletes **no** files — output filenames carry no tag, so the images are shared with every other Project |
| `GET /api/cards/status` | Card corpus status: what's imported locally (dataset, updated_at, count) and whether an import is running. Purely local — a file-existence + schema check and a meta read, **no network** — because the client polls this on its launch path; the live catalog fetch it used to bundle in now happens only inside an import job |
| `POST /api/cards/import` | Start a bulk import job (`{dataset: "default_cards" \| "all_cards"}`, 202 + job id; 409 while one runs). Background thread + in-memory registry (`card_jobs.py`), same polling idiom as the PDF render jobs |
| `GET /api/cards/import/{job_id}` | Import job progress: phase (checking/downloading/importing/finalizing), bytes, rows |
| `POST /api/cards/import/{job_id}/cancel` | Ask a running import to stop at its next chunk/batch boundary |
| `DELETE /api/cards/database` | Remove the imported corpus (file + WAL/SHM); 409 while an import job is running. The sidebar panel's "Delete card database" |
| `GET /api/cards/languages` | The full Scryfall language list (English first) — the import-language dropdown's options, independent of what corpus is imported: the dropdown is a request, resolution answers from corpus or live API |
| `GET /api/cards/variants` | Every printing of one card (shared `oracle_id`), anchored by scryfall_id / set+collector / name — the change-printing picker's contents. Corpus-only by design: 404s with an "import first" hint rather than falling back live |
| `GET /api/health` | Supervisor readiness probe |
| `POST /api/backs/{hash}` | Sync one Back Image's bytes to this server (raw body, not multipart — one file, no other fields, and a `Form`/`File` route would need `python-multipart`, whose absence would stop the server booting at all). Idempotent, and the hash is verified against the bytes: a content-addressed store that accepts mismatched bytes lies about every later lookup |
| `GET /api/backs/{hash}` | Does this server hold those bytes, and how sharp are they. The client calls it before every sync so an unchanged back costs one small GET rather than a multi-MB POST. Back Images are never upscaled — see ADR-0003 for why that asymmetry with card art is deliberate |
| `DELETE /api/backs/{hash}` | Remove a Back Image from this server entirely. The client's library copy is canonical and untouched |
| `GET /api/version` | The server's release version (`proxy_scaler.__version__`), for the client's drift warning — Remote mode means client and server are updated on different machines. Clients tolerate its absence (older servers 404) |

## Frontend data flow (`desktop/frontend/src`)

- `api/project.ts` — `invoke()` wrappers for the Rust commands above.
- `api/generation.ts` — `fetch()` against whichever host
  `connection.tsx`'s Local/Remote toggle currently points at. Both
  re-exported from a combined `api` object so page components don't care
  which transport a given call uses.
- `mergeCardStatus.ts` — the TypeScript port of the old
  `services/decklist.py` merge logic; combines a local project's card
  list with a generation server's task/gallery status by identity key.
- `DecklistPage.tsx` — two independent queries instead of one: local
  project cards (`invoke`-based, no polling — can't go stale behind your
  back, only invalidated on explicit mutation) and generation status
  (`fetch`-based, 3s poll — the half that's actually live), merged via
  `mergeCardStatus.ts`.
- `BacksPage.tsx` — the Back Library: an entirely local list (`invoke`),
  usable with no generation server reachable at all. A back's bytes never
  pass through the webview on the way out — `sync_back_image` uploads
  from Rust, for the same reason `main.rs::download_to_file` downloads
  there.
- `connection.tsx` — switching Local/Remote generation no longer touches
  project data at all. `switchTo` only invalidates generation-scoped
  query keys (`tasks`, `gallery`, `worker-status`, `pdf-preview`) —
  it does *not* clear the whole query cache or remount `ProjectProvider`,
  since project data is untouched by which generation server is
  connected.

## A concrete consequence worth knowing

See [ADR-0001](./docs/adr/0001-split-project-and-generation-concerns.md)'s
Consequences section. This is why the "connection lost" dialog
(`components/ConnectionLostDialog.tsx`) only concerns in-flight
generation work, not project data: losing the thing your project lived
on used to mean losing your edits, and now it structurally can't,
because project data was never there to begin with.

Both dialogs used to hedge against that anyway, and no longer do.
`ConnectionLostDialog` offered a save button and `SwitchServerDialog`
offered save-before-switching (with a `canSave` prop plumbed through
`ServerSwitcher.tsx`); both are gone, along with the strings that framed
a connection event as a threat to project data. Nothing is unsaved on
either path — for named Projects or Unnamed ones — so there is nothing
to offer. If a future dialog reaches for `useProject()` to warn about
losing work over a connection event, that is the mistake this section
exists to catch.
