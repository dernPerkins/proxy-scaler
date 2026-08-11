# Proxy Scaler

Desktop app for building Magic: The Gathering proxy decklists and generating print-ready card images/PDFs, split between project data that always lives on the client and card generation that can run locally or on a remote host.

## Language

**Project**:
A decklist workspace — its raw text, unresolved card list, and per-project generation preferences (model, DPI targets, tile size). Always lives on the machine running the desktop client, never on a generation server.
_Avoid_: Deck, workspace

**project_tag**:
An opaque UUID a Project mints once and attaches to every generation request, used purely to scope/filter that project's tasks and images. Deliberately not a foreign key — the generation side has no notion of a "projects" table.
_Avoid_: project_id (as a relational/FK concept)

**Generation server**:
The concern that resolves card names against Scryfall, downloads and upscales card images, and assembles PDFs. Has no concept of Projects — every request is scoped by `project_tag` alone.
_Avoid_: Backend, server (ambiguous on its own)

**Local / Remote**:
The user-facing toggle selecting which Generation server instance handles requests — an embedded sidecar on this machine (Local) or a separate host reached over the network (Remote). Never affects Project data.

**Resolve**:
Looking up a Project's raw decklist entries against Scryfall for display only — canonical names, catching typos — without enqueuing generation work or writing anything.
_Avoid_: Lookup, validate

**Generate**:
Submitting a Project's card list (plus its `project_tag`) to enqueue the download+upscale pipeline as tasks. Resolves internally first if the list hasn't already been resolved.
_Avoid_: Render, export
