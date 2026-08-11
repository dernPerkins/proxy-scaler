# Split project management from generation, connected only by project_tag

Project data (decklists, settings) and card generation (Scryfall resolution, download/upscale, PDF assembly) used to live in one Python FastAPI process with one shared SQLite database. They're now two independent concerns: Project management is native Rust, in-process in the Tauri app — always local, no network calls, no spawned process — while Generation stays the existing Python FastAPI service, reachable Local (embedded sidecar) or Remote. The two only communicate via an opaque `project_tag` UUID a Project mints once — never a foreign key — because project data has no reason to live on a generation server the user may not control, while generation genuinely needs compute/network a client machine may lack. This was a reshape rather than a rewrite: no SQL join in the old code ever spanned project and generation data, so the split was mechanical, not a logic change.

## Consequences

Project management (create a project, edit a decklist, save) now works identically whether or not any generation server is reachable, including while a configured Remote host is down — project data was never dependent on it to begin with.
