# Projects are optional, and promotion preserves project_tag

You used to have to name and save a project before the app would let you do anything — `ProjectContext` threw `"Save the project before importing a decklist."` and the import box stayed hidden until a project row existed. Now an import with no project lazily creates an **Unnamed Project**: an ordinary `projects` row whose `name` is the empty string, with a real id, a real `tag`, and its own persisted cards and settings. Naming it later is *promotion*, not creation: an `UPDATE ... SET name = ?` that never touches `tag`, so every image already generated under that tag stays attached to the project you just named.

## Considered options

**Rejected: mint a new tag on naming, and regenerate.** Treating the unnamed session as a scratchpad and the named project as a fresh thing would have made naming an INSERT — the obvious shape, and the one a reader expects. It costs the user everything they generated before they named: every image is scoped by `project_tag`, so a new tag orphans the lot, and the whole set wants re-upscaling. Upscaling is the expensive operation this app exists to run; charging for it a second time as the price of typing a name inverts the entire point of making projects optional. Promotion-by-UPDATE is why saving looks surprising in the code, and this is the reason it's worth the surprise.

**Rejected: a schema change.** No new table, no `is_unnamed` flag, no nullable name. `name` is `TEXT NOT NULL UNIQUE` already, and `''` can belong to exactly one row — so the existing constraint *is* the enforcement of "at most one Unnamed Project exists", for free and without a migration. The same constraint does double duty as the collision check when a name is finally typed. The cost is that hiding the row from the picker is carried by convention: `list_projects` says `WHERE name <> ''`, and a future listing query that forgets it will surface a blank-named row.

## Consequences

**Hard to reverse.** Once users have named projects holding images generated before the name, the tag-preserving UPDATE is load-bearing history, not just current behaviour.

**The macOS Cmd+Q gap is deliberate, not a bug.** Closing the window with cards in an Unnamed Project prompts, offering "Name & save" or "Not now". macOS Cmd+Q and menu Quit fire no window close event at all, and the Ctrl+C path never sees window events, so both skip the prompt entirely. That is safe rather than merely tolerated: skipping produces exactly the "Not now" outcome — the work is already persisted, because settings and cards write through on change. Nothing on the shutdown path fires discard HTTP, so nothing races `std::process::exit`. **Do not "fix" this** by reaching for the blocking dialog APIs; they deadlock on the main thread, and `prevent_close()` after an `await` is a silent no-op (`tauri-runtime-wry` reads the decision with `try_recv()` the instant the handler returns).

**Refines ADR-0001.** That ADR says a Project "mints once". Read it as once per *row*: a row can be created, discarded via New, and recreated at the next write, each time with a fresh tag. Nothing about the split changes — the tag is still opaque, still not a foreign key. (It is `lower(hex(randomblob(16)))` — 128 random bits in hex, not a formatted UUID, whatever ADR-0001 calls it.)

**Accepted residual costs**, each decided deliberately rather than overlooked. The intended home for the first two is the images/project manager planned as separate work — per-project image counts, disk usage, and surfacing unclaimed images:

1. A running upscale can't be cancelled, so it can finish and write a gallery row for a tag that was already discarded.
2. Discard cleanup (`POST /api/tags/{project_tag}/discard`) is fire-and-forget and never retried — each generation server owns its own database, so the call can be aimed at a host that never held those records, and a retry queue would need `(host, tag)` pairs and still fail permanently once a host is gone.
3. The pre-probe model fallback can stick across a relaunch: settings persist on change, so if the row is first written before the slow `/api/device` probe answers, the fallback model is stored and the next launch treats it as deliberate. Recoverable via New or a manual pick.
4. No undo on rename — with no Save button, a stray keystroke in a focused name field renames a project.
5. `WHERE name <> ''` is convention, not schema (above).
