# Architecture

Living document — re-read this before touching anything architecture-level,
and update it in the same change that shifts the architecture. Out of date
docs are worse than no docs; keep this current or delete the section.

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

Two independent concerns used to live in one Python FastAPI process with
one SQLite database. They're being pulled apart because project data
(your decklists, your settings) has no reason to live on a Remote
generation server you don't control, while generation (Scryfall lookups,
downloads, GPU upscaling) genuinely needs compute/network a client
machine may not have:

- **Project management** — project CRUD, decklist text, the (unresolved)
  card list, per-project generation preferences. Always local to the
  machine running the desktop client, regardless of where generation
  happens. Native Rust, in-process in the Tauri app — not a spawned
  process, not a network call.
- **Generation** — Scryfall resolution, the download+upscale pipeline,
  the task queue, generated images, PDF assembly. Runs wherever the
  user's Local/Remote toggle points: the embedded sidecar on this
  machine, or a Remote host (their own machine via the server app, or a
  Linux box via the `.deb`).

These two only ever talk to each other via a `project_tag` — an opaque
UUID the client mints once per local project and includes on every
generation request, purely for scoping/filtering ("list tasks for this
project"). It is deliberately not a foreign key into anything; the
generation side doesn't know or care that a `projects` table exists.
This mirrors what `generation_tasks.project_id` already was before the
split (nullable, never joined, already-soft) — the split just makes that
looseness the *only* relationship, instead of an accident of a shared
database.

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
                          │  - Scryfall resolution                 │
                          │  - download + upscale pipeline         │
                          │  - generation_tasks queue               │
                          │  - project_gallery_items (images)      │
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
- **`project_gallery_items` already redundantly stored every identity
  field** on each row. Its only real dependency on `project_cards` was
  the `card_id` foreign key, which became a plain `project_tag TEXT`
  column — a small, mechanical schema change.

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

1. User edits decklist text in a project. The Rust project store parses
   it locally (`parse_decklist_text` — a direct Rust port of the old
   `proxy_scaler/decklist.py`, two regexes, no network) and stores the
   raw, **unresolved** lines (quantity + name + optional set/collector).
2. The frontend calls `POST /api/resolve` (debounced, on decklist
   change) against whichever generation server is configured, purely for
   *display* — canonical card names, catching typos/invalid entries
   before committing to real work. No DB writes on the generation side,
   no enqueue.
3. On **Generate**, the frontend sends the project's card list (resolved
   or raw — the endpoint resolves internally if needed) plus the
   project's `project_tag` to `POST /api/generate`. The generation server
   resolves (if not already), downloads, and upscales each face as one
   pipeline per task — not three separate round trips.
4. Status polling (`GET /api/tasks?project_tag=...`) and generated images
   (`GET /api/gallery/{id}/...`) are scoped by that same tag. The
   frontend merges this generation-side data with its own local project
   card list client-side, using `mergeCardStatus.ts`.

## Endpoint / command inventory

### Rust (Tauri commands — `desktop/src-tauri/src/project_store.rs`)

All project management. Local-only, no network, callable regardless of
generation-server state.

| Command | Purpose |
|---|---|
| `create_project` | New project (name, empty decklist, default settings) |
| `list_projects` | Summaries for the project picker |
| `get_project` | Full record: name, decklist text, unresolved card list, settings |
| `update_project` | Rename / update settings |
| `delete_project` | Delete one project (cascades to its cards) |
| `clear_all_projects` | Delete everything, confirm-gated |
| `set_decklist_text` | Replace decklist text; re-parses and re-stores card lines |
| `remove_card` | Drop one parsed line from a project |
| `get_last_project_id` / `set_last_project_id` | Auto-load-on-launch support |

Project settings that live here: `model`, `dpi_targets`, `skip_existing`,
`tile_size` — genuine per-project preferences. Deliberately **not**
here: `output_dir`/`cache_dir`/`weights_dir`. Those are filesystem paths
meaningful only on whichever machine is generating — a path valid on
your Mac is meaningless against a Remote Linux host — so they're
generation-server-side defaults instead of portable project data.

### HTTP (generation server — `proxy_scaler/api/`)

No "projects" router. Every request is scoped by an opaque `project_tag`
string, not a database relationship.

| Method + path | Purpose |
|---|---|
| `POST /api/resolve` | Scryfall-resolve raw entries for display only; no writes |
| `POST /api/generate` | Resolve (if needed) + enqueue download+upscale per face |
| `GET /api/tasks?project_tag=` | List tasks for a project |
| `GET /api/tasks/{id}` | Single task status |
| `POST /api/tasks/{id}/cancel` | Cancel a queued/running task |
| `GET /api/worker/status` | Is the background worker alive |
| `GET /api/gallery?project_tag=` | List generated images for a project |
| `GET /api/gallery/{id}/original` \| `/full` | Serve image bytes |
| `POST /api/pdf` | Assemble a PDF from `project_tag` + layout + card list **with quantities** (quantity is project data the generation server has no other way to know) |
| `GET /api/models` | Enumerate upscale models (static, unchanged) |
| `POST /api/generated-data/clear` | Wipe output/cache dirs on the generation machine |
| `GET /api/health` | Supervisor readiness probe |

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
- `connection.tsx` — switching Local/Remote generation no longer touches
  project data at all. `switchTo` only invalidates generation-scoped
  query keys (`tasks`, `gallery`, `worker-status`, `pdf-preview`) —
  it does *not* clear the whole query cache or remount `ProjectProvider`,
  since project data is untouched by which generation server is
  connected.

## A concrete consequence worth knowing

Project management (create a project, edit a decklist, save) now works
identically whether or not any generation server is reachable — including
while a configured Remote host is down. This is why the "connection
lost" dialog (`components/ConnectionLostDialog.tsx`) only concerns
in-flight generation work, not project data: losing the thing your
project lived on used to mean losing your edits, and now it structurally
can't, because project data was never there to begin with.
