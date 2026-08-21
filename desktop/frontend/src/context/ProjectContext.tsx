import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi } from "../api/project";
import { getConnectionMode, getProbedDevice, subscribeProbedDevice } from "../config";
import type {
  CardRow,
  LoadedProject,
  ProjectSettings,
  ResolvedImportCard,
} from "../api/project";

// Heavy/quality-first vs. light/fast. Named rather than repeated inline so
// the branching below reads as "which class of hardware is this."
const HEAVY_MODEL = "ultrasharp_v2";
const FAST_MODEL = "realesrgan_anime_fast";

// Prefers the real answer from connection.tsx's /api/device probe
// (fired once per connect()/switchTo(), see config.ts::probedDevice) —
// neither Local nor Remote implies anything about the actual hardware
// behind the connected server. Falls back to the old mode-based guess
// only while that probe hasn't answered yet (or failed): local runs
// on-device, so a fast/light model is the safer default; a remote
// server is assumed to have real GPU headroom.
//
// "Has a GPU" is not by itself enough to pick the heavy model. The
// backend matters:
//
// - cuda (also ROCm, which reports through the same torch.cuda APIs):
//   the heavy model, unchanged — this is the hardware it was chosen for.
// - mps (Apple Silicon): the fast model. MPS is a real GPU, but on the
//   heavy transformer/attention models it is slow enough on unified
//   memory that the default felt broken on an M2. This is the bug this
//   branch exists to fix; before `backend` existed, MPS was
//   indistinguishable from CUDA here.
// - privateuseone (torch-directml, AMD/Intel on Windows): the heavy
//   model, matching today's behavior. These are discrete cards with
//   their own VRAM, and nobody has reported a problem — changing it
//   would be an untested regression risk against the GPU-detection work
//   that just shipped, not a fix.
// - anything else, including an older server that doesn't send `backend`
//   at all: fall back to the coarse `kind`, i.e. exactly the pre-existing
//   gpu-or-not behavior. Never let an unrecognized backend name silently
//   downgrade a real GPU box.
function recommendedDefaultModel(): string {
  const device = getProbedDevice();
  if (device !== null) {
    if (device.backend === "mps") return FAST_MODEL;
    return device.kind === "gpu" ? HEAVY_MODEL : FAST_MODEL;
  }
  return getConnectionMode() === "local" ? FAST_MODEL : HEAVY_MODEL;
}

// Settings writes are debounced because their callers are sliders and
// number inputs — the Decklist sidebar's DPI/tile controls, the whole of
// PdfPage's layout panel (`updateLayout`) — which fire a setter per frame
// of a drag. One UPDATE per frame is not something to ask SQLite for; one
// trailing write per gesture is. Keyed on nothing: settings are a single
// object, so the last write wins by construction.
const SETTINGS_WRITE_DEBOUNCE_MS = 400;

function getDefaultSettings(): ProjectSettings {
  return {
    model: recommendedDefaultModel(),
    dpi_targets: [1200],
    skip_existing: true,
    tile_size: 0,
    page_width_mm: 210,
    page_height_mm: 297,
    cols: 3,
    rows: 3,
    bleed_mm: 1.0,
    spacing_x_mm: 0,
    spacing_y_mm: 0,
    offset_x_mm: 0,
    offset_y_mm: 0,
    guide_width_pt: 0.75,
    guide_length_mm: 2.75,
    export_dpi: 1200,
    show_cut_lines: true,
    preferred_dpi: null,
    preferred_model: null,
    preferred_lang: "en",
    lang_any: false,
  };
}

interface ProjectContextValue {
  projectId: number | null;
  /** Opaque tag passed to the generation server to scope tasks/gallery —
   *  see ARCHITECTURE.md. Null only until a row exists; the first import
   *  creates one (the Unnamed Project), named or not. */
  projectTag: string | null;
  /** The *stored* name — `''` for the Unnamed Project. Not what is
   *  currently in the bar's name field: that is local to the field until
   *  a pause commits it through `rename` — see ProjectBar.tsx and
   *  .scratch/optional-projects/spec.md §5.4. */
  projectName: string;
  settings: ProjectSettings;
  setSettings: (updater: ProjectSettings | ((s: ProjectSettings) => ProjectSettings)) => void;
  decklistText: string;
  cards: CardRow[];
  /** Parses `text` and adds any new cards to `cards` — additive, never
   *  removes an existing card (see project_store.rs::import_decklist_text).
   *  Also remembers `text` itself as decklistText — not shown anywhere,
   *  but Save As replays it to copy the deck into the new project.
   *
   *  With no project yet, this is what creates one: the Unnamed Project is
   *  born on the first import, and projectId/projectTag are non-null from
   *  then on. Resolves on success so the import box can clear itself;
   *  rejects on failure (the error is also surfaced via `error`). */
  importDecklistText: (text: string) => Promise<void>;
  importingDecklistText: boolean;
  /** The resolve-gated import's persist step: inserts already-resolved
   *  (fully pinned) cards in one transaction, deduped like
   *  importDecklistText, and remembers `text` as decklistText. Creates the
   *  Unnamed Project on first use the same way. The caller (DecklistPage)
   *  owns the parse + resolve halves. */
  importResolvedCards: (text: string, cards: ResolvedImportCard[]) => Promise<void>;
  importingResolvedCards: boolean;
  removeCard: (cardId: number) => void;
  /** Sets a card's copy count. Values below 1 are clamped to 1 — removal
   *  is removeCard's job. */
  setCardQuantity: (cardId: number, quantity: number) => void;
  /** Changes one card to a different printing (picked from the server's
   *  variants endpoint): pins scryfall_id and refreshes the display cache
   *  (name/set/collector/lang). Resolves once stored, so callers can
   *  invalidate their server-side status queries after it lands. */
  setCardPrinting: (
    cardId: number,
    printing: {
      scryfallId: string;
      name: string;
      setCode: string;
      collectorNumber: string;
      lang: string;
      printedName: string | null;
    },
  ) => Promise<void>;
  /** Persists post-import resolve results in one batch — pins each card's
   *  scryfall_id and fills in concrete set/collector for name-only lines.
   *  See DecklistPage's eager resolve effect. */
  applyCardResolutions: (
    updates: {
      card_id: number;
      scryfall_id: string;
      name: string;
      set_code: string;
      collector_number: string;
      lang: string;
      printed_name: string | null;
    }[],
  ) => Promise<void>;
  /** Whether the project has a name. Deliberately not "is saved": since
   *  settings began writing through (.scratch/optional-projects/spec.md
   *  §5.2) everything is saved, named or not — what varies is whether you
   *  can find it again. */
  isNamed: boolean;
  /** Names the project, in place: the promotion from Unnamed Project to
   *  named one, and an UPDATE rather than an INSERT, so the tag — and
   *  with it every image already generated — survives it.
   *
   *  Awaited by its one caller, the bar's name field, which renders the
   *  rejection message (a collision, typically) beside the field and
   *  keeps the typed text. Rejecting rather than routing through `error`
   *  keeps a name the user is still editing out of the bar's general
   *  error line. */
  rename: (name: string) => Promise<void>;
  saveAs: (name: string) => void;
  /** Lands *every* debounced write now — settings here, and the name the
   *  bar's field has queued (registerNameCommitFlush) — and resolves
   *  once they, and anything already in flight ahead of them, have reached
   *  the store. For the way out of the app: the quit prompt
   *  (QuitPrompt.tsx) promises everything is already stored, and a change
   *  made inside the last debounce window would otherwise still be sitting
   *  in a timer when the process exits. */
  flushPendingWrites: () => Promise<void>;
  /** The name field's debounce lives in ProjectBar, not here, so the bar
   *  hands over a way to land whatever it has queued: called with a flush
   *  on mount, with null on unmount. Only one field exists, so the last
   *  registration wins.
   *
   *  The flush must not reject — flushPendingWrites is awaited on the
   *  shutdown path, where a throw would cost the quit its answer. */
  registerNameCommitFlush: (flush: (() => Promise<void>) | null) => void;
  /** New *is* discard: from an Unnamed Project it deletes the row (and
   *  fires the tag's discard at the connected server), from a named Project
   *  it only detaches to a blank slate. See spec §5.6.
   *
   *  Asks nothing itself, and is synchronous because of it. §5.6's confirm
   *  is rendered by the button that calls this — see `newWouldDiscard` and
   *  ProjectBar.tsx.
   *
   *  So this discards unconditionally, and any *new* caller owes the user
   *  the same question: check `newWouldDiscard` first. It cannot ask here
   *  — a React modal makes the answer asynchronous, and the bar depends on
   *  this being synchronous to clear its name field behind the discard
   *  without a queued commit landing in between. */
  createNew: () => void;
  /** Whether the next `createNew` would destroy something: an Unnamed
   *  Project holding cards, whose row and gallery entries go with it.
   *  §5.6's "holding cards" test, kept here beside the function it
   *  describes rather than re-derived by whoever puts the confirm on
   *  screen. Every other New is a detach and asks nothing. */
  newWouldDiscard: boolean;
  load: (id: number) => void;
  remove: (id: number) => void;
  error: string | null;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function useProject(): ProjectContextValue {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error("useProject must be used within ProjectProvider");
  return ctx;
}

// Replaces two things the old Streamlit version needed and this one
// doesn't: the `_persist_<key>` widget-mirroring hack in ui/projects.py
// (only needed because Streamlit drops an inactive tab's widget state
// each rerun — a React SPA doesn't unmount inactive pages' state at all),
// and the pending-flag-applied-next-rerun state machine for New/Load/
// Delete (only needed because Streamlit can't safely mutate widget-bound
// state mid-run — a plain onClick handler can just call setState
// directly).
export function ProjectProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [projectId, setProjectId] = useState<number | null>(null);
  const [projectTag, setProjectTag] = useState<string | null>(null);
  const [projectName, setProjectName] = useState("");
  const [settings, setSettings] = useState<ProjectSettings>(getDefaultSettings);
  const [decklistText, setDecklistTextState] = useState("");
  const [cards, setCards] = useState<CardRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Whether settings.model is still an unreviewed default, i.e. safe for
  // the GPU probe below to revise. Flipped by any deliberate model change
  // — the user picking one, or a saved project supplying its own.
  const modelIsDefault = useRef(true);
  // projectId and settings are mirrored into refs because the write paths
  // below are async: a setState lands on a later render, so a write firing
  // in the same tick that changed them would otherwise read stale values —
  // ask for a second Unnamed Project, or persist the settings from before
  // the edit that scheduled the write.
  const projectIdRef = useRef<number | null>(null);
  const settingsRef = useRef<ProjectSettings>(settings);
  // The debounced settings write, and the project it is for. The target is
  // captured when the write is scheduled, not read when it fires, so an
  // edit made in one project can never land on whichever project is open
  // 400ms later. A null target means the edit was made before any row
  // existed, and firing it is what creates the Unnamed Project.
  const pendingSettingsWrite = useRef<{ projectId: number | null; settings: ProjectSettings } | null>(
    null,
  );
  const settingsWriteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settingsWriteChain = useRef<Promise<void>>(Promise.resolve());
  // A discard's row delete, while it is in flight. ensureProjectRow waits on
  // it so an import arriving in the same breath as New can't have its
  // get_or_create answered with the row being deleted — that would hand back
  // the discarded tag and then delete the row the import is writing into.
  // Never rejects; discardUnnamedRow reports the failure itself, and a
  // delete that failed leaves the row for get_or_create to find, which is
  // the honest answer to "what row is there now".
  const rowDeleteInFlight = useRef<Promise<void>>(Promise.resolve());
  // Lands the name the bar's field has queued, or null while no bar is
  // mounted — see registerNameCommitFlush. Held rather than owned because
  // the debounce belongs to the field: what is typed is the field's until
  // it commits, and the context only ever holds the stored name.
  const nameCommitFlush = useRef<(() => Promise<void>) | null>(null);

  function adoptProjectId(id: number | null) {
    projectIdRef.current = id;
    setProjectId(id);
  }

  function applySettings(next: ProjectSettings) {
    settingsRef.current = next;
    setSettings(next);
  }

  // The row every write needs, created on first demand rather than at
  // launch: the Unnamed Project is born the first time there is something
  // to store — a decklist import, or a settings change with no cards at
  // all — and an app that has been installed and never used holds no row.
  //
  // Shared by both write paths, so whichever the user reaches first is the
  // one that creates the row.
  async function ensureProjectRow(): Promise<number> {
    const existing = projectIdRef.current;
    if (existing != null) return existing;
    await rowDeleteInFlight.current;
    const unnamed = await projectApi.getOrCreateUnnamedProject();
    // Adopted straight away rather than in a mutation's onSuccess: the row
    // exists in the store from this point on whether or not the write that
    // follows succeeds, and state that says otherwise would strand it.
    // projectName is left alone — the row's name really is '', and the
    // bar's field holds whatever the user is in the middle of typing.
    adoptProjectId(unnamed.id);
    setProjectTag(unnamed.tag);
    // The Unnamed Project is what the next launch should restore.
    await projectApi.setLastProjectId(unnamed.id);
    return unnamed.id;
  }

  function discardPendingSettingsWrite() {
    if (settingsWriteTimer.current != null) {
      clearTimeout(settingsWriteTimer.current);
      settingsWriteTimer.current = null;
    }
    pendingSettingsWrite.current = null;
  }

  function scheduleSettingsWrite(next: ProjectSettings) {
    // Restarted, not extended: a drag schedules one write, at its end.
    discardPendingSettingsWrite();
    pendingSettingsWrite.current = { projectId: projectIdRef.current, settings: next };
    settingsWriteTimer.current = setTimeout(flushSettingsWrite, SETTINGS_WRITE_DEBOUNCE_MS);
  }

  function flushSettingsWrite() {
    const pending = pendingSettingsWrite.current;
    discardPendingSettingsWrite();
    if (pending == null) return;
    // Chained rather than fired loose so two writes can't overlap and leave
    // the store holding the older object.
    settingsWriteChain.current = settingsWriteChain.current.then(async () => {
      let target = pending.projectId;
      try {
        target ??= await ensureProjectRow();
        // The empty name means "settings only": update_project keeps the
        // stored name when handed a blank one (project_store.rs::
        // update_project_row), so a slider drag can never also commit
        // whatever half-typed text is sitting in the bar's name field.
        // Naming stays an act of its own.
        await projectApi.updateProject(target, "", pending.settings);
      } catch (err) {
        // A write already in flight can't be called back, so it can land
        // against a row Delete has just removed — update_project fails
        // reading the summary back. That's the user's own doing and not
        // something to report; only a failure against the project still
        // open in front of them is worth an error line.
        if (target != null && target !== projectIdRef.current) return;
        // Otherwise: must not throw into the render path — the user's edit
        // stands in React state exactly as typed, and the failure surfaces
        // through the same error line every other project operation uses.
        setError(err instanceof Error ? err.message : String(err));
      }
    });
  }

  // Called by anything that swaps out the current project — load, New,
  // delete, Save As. A pending edit was made *in* the outgoing project, so
  // it is flushed rather than dropped when it has a row to land on, and
  // discarded when it doesn't: its target was a blank slate that this
  // transition has just replaced, and creating a row for it now would leave
  // a stray Unnamed Project behind.
  function settleSettingsWriteBeforeTransition() {
    if (pendingSettingsWrite.current?.projectId != null) flushSettingsWrite();
    else discardPendingSettingsWrite();
  }

  // Exposed instead of the raw setter so the model can't be quietly
  // overwritten after the user has expressed a preference. Every other
  // setting passes through untouched.
  //
  // This is also the write: settings reach SQLite from here, not from an
  // explicit Save, for the Unnamed Project and named Projects alike (spec
  // §5.2). One ProjectSettings object covers the sidebar and the entire
  // PDF layout, so PDF-tab tweaks now persist too.
  function updateSettings(
    updater: ProjectSettings | ((s: ProjectSettings) => ProjectSettings),
  ) {
    const prev = settingsRef.current;
    const next = typeof updater === "function" ? updater(prev) : updater;
    if (next.model !== prev.model) modelIsDefault.current = false;
    applySettings(next);
    scheduleSettingsWrite(next);
  }

  function applyLoaded(project: LoadedProject) {
    settleSettingsWriteBeforeTransition();
    adoptProjectId(project.id);
    setProjectTag(project.tag);
    setProjectName(project.name);
    applySettings(project.settings);
    // A saved project's stored model is an explicit choice, whatever it
    // happens to be — never second-guess it when the probe lands.
    modelIsDefault.current = false;
    setDecklistTextState(project.import_decklist_text);
    setCards(project.cards);
    setError(null);
  }

  // Naming, which is all that is left of what used to be Save. Where Save
  // branched create-vs-update on projectId and called create_project when
  // there wasn't one, this only ever updates: minting a fresh row here
  // would mint a fresh tag with it and orphan every image already
  // generated under the old one — the tag is minted by the INSERT itself
  // (project_store.rs::insert_project). Nothing about naming may move it.
  const renameMutation = useMutation({
    mutationFn: async (name: string) => {
      const trimmed = name.trim();
      // Clearing the field is ignored at the field (spec §5.4) and blank
      // means "settings only" to update_project, so this is unreachable
      // rather than a policy — it just refuses to write nothing.
      if (!trimmed) throw new Error("Project name is required.");
      // Almost always a no-op since the first import began creating the
      // row: by the time there is anything worth naming, it exists. A name
      // typed into an app holding nothing at all is allowed to be what
      // creates it — a name is something to store like any other.
      //
      // So a name that then collides leaves the row behind. That residue
      // is the Unnamed Project, the single row get_or_create hands back to
      // every later write anyway, and the picker never shows it.
      const id = await ensureProjectRow();
      // settingsRef, not settings: a settings change made in the same tick
      // as the commit is in the ref already, while state is a render behind.
      // The pending settings write is deliberately left alone — it belongs
      // to the settings gesture, carries these same values, and
      // update_project keeps the stored name when handed a blank one.
      const summary = await projectApi.updateProject(id, trimmed, settingsRef.current);
      await projectApi.setLastProjectId(summary.id);
      return summary;
    },
    onSuccess: (summary) => {
      // The tag is untouched by construction — this was an UPDATE of the
      // row the session is already holding.
      setProjectName(summary.name);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    // No onError: the message is the caller's, surfaced beside the name
    // field rather than in the bar's general error line.
  });

  const saveAsMutation = useMutation({
    mutationFn: async (name: string) => {
      const trimmed = name.trim();
      if (!trimmed) throw new Error("Name required.");
      // The copy carries the current settings; the original keeps them too.
      settleSettingsWriteBeforeTransition();
      const summary = await projectApi.createProject(trimmed);
      await projectApi.updateProject(summary.id, summary.name, settings);
      if (decklistText) {
        await projectApi.importDecklistText(summary.id, decklistText);
      }
      await projectApi.setLastProjectId(summary.id);
      return projectApi.getProject(summary.id);
    },
    onSuccess: (project) => {
      applyLoaded(project);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const loadMutation = useMutation({
    mutationFn: async (id: number) => {
      const project = await projectApi.getProject(id);
      await projectApi.setLastProjectId(id);
      return project;
    },
    onSuccess: applyLoaded,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => projectApi.deleteProject(id),
    onSuccess: (_data, id) => {
      if (projectId === id) {
        // Discarded rather than settled: the row this write targets is the
        // one that was just deleted.
        discardPendingSettingsWrite();
        adoptProjectId(null);
        setProjectTag(null);
        setProjectName("");
        setDecklistTextState("");
        setCards([]);
      }
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const importResolvedCardsMutation = useMutation({
    mutationFn: async (args: { text: string; cards: ResolvedImportCard[] }) => {
      // Same born-here rule as the legacy import below: with no project
      // yet, the first successful import creates the Unnamed Project.
      const id = await ensureProjectRow();
      const newCards = await projectApi.importResolvedCards(id, args.text, args.cards);
      return { text: args.text, newCards };
    },
    onSuccess: ({ text, newCards }) => {
      setDecklistTextState(text);
      setCards(newCards);
      setError(null);
    },
    onError: (err: Error) => setError(err.message),
  });

  const importDecklistMutation = useMutation({
    mutationFn: async (text: string) => {
      // The row can be born here. With no project yet, importing creates
      // the Unnamed Project rather than refusing — naming is optional and
      // comes later, if at all (spec §5.1).
      //
      // Note the returned `id`, not projectId: setProjectId lands on a
      // later render, so reading it back in this tick would still see null.
      const id = await ensureProjectRow();
      const newCards = await projectApi.importDecklistText(id, text);
      return { text, newCards };
    },
    onSuccess: ({ text, newCards }) => {
      setDecklistTextState(text);
      setCards(newCards);
      setError(null);
    },
    onError: (err: Error) => setError(err.message),
  });

  const removeCardMutation = useMutation({
    mutationFn: (cardId: number) => projectApi.removeCard(cardId),
    onSuccess: (_data, cardId) => {
      setCards((prev) => prev.filter((c) => c.id !== cardId));
    },
  });

  const setCardPrintingMutation = useMutation({
    mutationFn: async (args: {
      cardId: number;
      printing: {
        scryfallId: string;
        name: string;
        setCode: string;
        collectorNumber: string;
        lang: string;
        printedName: string | null;
      };
    }) => {
      await projectApi.setCardPrinting(args.cardId, args.printing);
      return args;
    },
    onSuccess: ({ cardId, printing }) => {
      setCards((prev) =>
        prev.map((c) =>
          c.id === cardId
            ? {
                ...c,
                scryfall_id: printing.scryfallId,
                name: printing.name,
                set_code: printing.setCode,
                collector_number: printing.collectorNumber,
                lang: printing.lang,
                printed_name: printing.printedName,
              }
            : c,
        ),
      );
    },
    onError: (err: Error) => setError(err.message),
  });

  const applyCardResolutionsMutation = useMutation({
    mutationFn: async (
      updates: {
        card_id: number;
        scryfall_id: string;
        name: string;
        set_code: string;
        collector_number: string;
        lang: string;
        printed_name: string | null;
      }[],
    ) => {
      await projectApi.setCardsResolution(updates);
      return updates;
    },
    onSuccess: (updates) => {
      const byId = new Map(updates.map((u) => [u.card_id, u]));
      setCards((prev) =>
        prev.map((c) => {
          const u = byId.get(c.id);
          return u
            ? {
                ...c,
                scryfall_id: u.scryfall_id,
                name: u.name,
                set_code: u.set_code,
                collector_number: u.collector_number,
                lang: u.lang,
                printed_name: u.printed_name,
              }
            : c;
        }),
      );
    },
    // Deliberately no onError surface: the eager resolve is best-effort
    // background work — a failure just leaves cards unpinned, and the next
    // project load retries.
  });

  const setCardQuantityMutation = useMutation({
    mutationFn: async ({ cardId, quantity }: { cardId: number; quantity: number }) => {
      await projectApi.setCardQuantity(cardId, quantity);
      // Mirror the Rust side's min-1 clamp so local state can't disagree
      // with what was actually stored.
      return { cardId, quantity: Math.max(1, quantity) };
    },
    onSuccess: ({ cardId, quantity }) => {
      setCards((prev) =>
        prev.map((c) => (c.id === cardId ? { ...c, quantity } : c))
      );
    },
  });

  // Deleting the Unnamed Project row is the whole of "discard" — there is
  // no separate mint step, because ensureProjectRow's get_or_create hands
  // back a fresh row, and with it a fresh tag, at the next import
  // (.scratch/optional-projects/decisions/03-tag-lifecycle-and-record-cleanup.md).
  function discardUnnamedRow(id: number, tag: string | null) {
    // Fired at the connected server once, and never retried. Each
    // generation server owns its own database, so this call can
    // legitimately be aimed at a host that never held these records —
    // generate on Remote, switch to Local, discard. A retry queue would
    // have to remember (host, tag) pairs and would still fail permanently
    // once a host is gone, so failure is simply accepted: the orphaned rows
    // it leaves are the business of the images manager the spec puts out of
    // scope (§9). Nothing here blocks the row delete or reaches the error
    // line, including "the server is stopped".
    //
    // Not literally "the server connected at this instant": request() waits
    // on config.ts's readiness gate first, so a discard fired while the
    // local sidecar is down parks there and can land on whatever the user
    // connects to next. Aiming at the wrong host is already the accepted
    // case above, so this needs no machinery of its own.
    if (tag) void generationApi.discardTag(tag).catch(() => {});
    // The local row is the part that has to happen. If this fails the row
    // survives holding its cards, the next import reopens it — old tag and
    // all, since get_or_create finds it still there — and the blank slate
    // in front of the user is a lie. So unlike the call above, this one is
    // reported; a failed delete is a failed discard, not a silent one.
    //
    // No ["projects"] invalidation: the picker lists named projects only
    // (project_store.rs::list_project_summaries filters `name <> ''`), so
    // an Unnamed Project leaving the store changes nothing it shows.
    rowDeleteInFlight.current = projectApi
      .deleteProject(id)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }

  // The same discard, aimed at an Unnamed Project the user is *not* in —
  // see createNew's else branch for why New has to make this call. Parked
  // in rowDeleteInFlight exactly like discardUnnamedRow's delete, so an
  // import arriving in the same breath can't have its get_or_create
  // answered with the row being deleted.
  function discardOrphanedUnnamedRow() {
    rowDeleteInFlight.current = projectApi
      .discardUnnamedProject()
      .then((tag) => {
        // Same best-effort, never-retried cleanup as discardUnnamedRow's:
        // the records belong to whichever server produced them, which is
        // not necessarily the one connected now.
        if (tag) void generationApi.discardTag(tag).catch(() => {});
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }

  // Whether New, from the project identified by `id`, is the discard rather
  // than a detach. "Unnamed Project" is a row with no stored name — the same
  // test as isNamed. With no row at all there is nothing to delete and this
  // is already a blank slate.
  //
  // A rename still in flight is treated as named even though projectName is
  // still '': the name lands a moment later, and deleting the row underneath
  // it would throw away a Project the user had just named. The reverse
  // mistake — detaching from a row whose rename then fails on a collision —
  // leaves an Unnamed Project the next import reopens, and loses nothing.
  //
  // Only the id is a parameter, because it is the only operand whose two
  // callers disagree: createNew passes the ref, current as of this tick,
  // while newWouldDiscard passes state, which is what the bar's button is
  // rendered against. projectName and the rename's pending flag are read
  // from the render closure either way — they move together with a render,
  // so there is no staler and fresher reading of them to choose between.
  // Narrowing `id` on the way out is what lets createNew hand it straight
  // to discardUnnamedRow.
  function isDiscard(id: number | null): id is number {
    return id != null && projectName.trim().length === 0 && !renameMutation.isPending;
  }

  // "New" and "discard" are one operation (spec §5.6). From a named Project
  // this keeps today's meaning — detach to a blank slate, leaving the named
  // Project itself alone. From an Unnamed Project it is the discard, and the
  // row goes with it: left behind it would strand its tag, and the next
  // import would reopen the old session instead of starting over.
  //
  // The confirm §5.6 puts in front of a discard that is holding cards is not
  // here: this stays synchronous, and the button renders the question (see
  // newWouldDiscard). By the time this is called the answer is already yes.
  function createNew(): void {
    const id = projectIdRef.current;
    const discarding = isDiscard(id);
    if (discarding) {
      // Discarded rather than settled: the row this write targets is the
      // one about to be deleted.
      discardPendingSettingsWrite();
      discardUnnamedRow(id, projectTag);
    } else {
      settleSettingsWriteBeforeTransition();
      // Detaching from a named Project is not by itself a blank slate: an
      // Unnamed Project row can already exist (left behind by switching
      // away from one via the picker, since New never ran on it), and
      // ensureProjectRow's get_or_create would hand that row — its cards,
      // its tag, its settings — to the "new" project at its first write.
      // The user would then import a decklist and watch a previous
      // session's cards appear alongside it.
      //
      // So New sweeps it away, unprompted. There is no discard confirm
      // here on purpose: the row being deleted is not the one on screen,
      // and New from a named Project has never asked about anything —
      // asking would be a question about a session the user left behind
      // and cannot see. (The confirm in front of a *self* discard, where
      // the cards being thrown away are the ones in front of them, is
      // unchanged — see newWouldDiscard.)
      discardOrphanedUnnamedRow();
    }
    adoptProjectId(null);
    setProjectTag(null);
    setProjectName("");
    applySettings(getDefaultSettings());
    modelIsDefault.current = true;
    setDecklistTextState("");
    setCards([]);
    setError(null);
  }

  // The debounce leaves a window — up to SETTINGS_WRITE_DEBOUNCE_MS — in
  // which a settings change exists only in React state. Quitting inside it
  // would lose the edit, which is exactly what "settings persist on change"
  // promises not to do, so hand the pending write off the moment the window
  // is on its way out. Both signals are best-effort: the webview may be
  // torn down before the invoke lands, and macOS Cmd+Q fires neither (spec
  // §6). They shrink the window; they don't close it. The quit path is
  // where a pending write is actually awaited (flushPendingWrites).
  useEffect(() => {
    function flushSettingsOnTheWayOut() {
      flushSettingsWrite();
    }
    // pagehide takes the name field's debounce with it; blur deliberately
    // does not. A settings value is complete at every keystroke, so landing
    // it early costs nothing — a name is composed over time, and committing
    // one because the user alt-tabbed would rename the project to whatever
    // half of it had been typed. Decision 08 accepts an accidental rename
    // from a stray keystroke in a focused field; losing window focus is not
    // that, and spec §5.4 wants a collision reported only once typing has
    // settled. pagehide means the document is going away, which is the one
    // moment a partial name beats no name at all.
    function flushEverythingOnTheWayOut() {
      flushSettingsWrite();
      // Same contract as the awaited call in flushPendingWrites, which
      // cannot be honoured by awaiting here: nothing may reject into a
      // listener.
      void nameCommitFlush.current?.().catch(() => {});
    }
    window.addEventListener("pagehide", flushEverythingOnTheWayOut);
    window.addEventListener("blur", flushSettingsOnTheWayOut);
    return () => {
      window.removeEventListener("pagehide", flushEverythingOnTheWayOut);
      window.removeEventListener("blur", flushSettingsOnTheWayOut);
      // A provider being torn down with a write still queued: land it now,
      // since nothing after this point will. Settings only, and not for
      // want of trying — ProjectBar unmounts before its provider does, and
      // takes both its queued commit and its registration with it, so
      // there is provably no name left here to land.
      flushSettingsWrite();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The /api/device answer arrives long after this provider mounts —
  // torch's cold import means the probe can take tens of seconds, while
  // the UI is interactive within about one. getDefaultSettings() therefore
  // runs while the probed device is still null and falls back to the mode-based
  // guess, which for Local mode assumes no GPU and picks the light
  // realesrgan model. On a real GPU box that's simply the wrong default,
  // and it was sticky: nothing ever revisited it.
  //
  // So revise it once the truth is known, but only where that's clearly
  // safe — an unsaved project whose model hasn't been touched. A saved
  // project or a deliberate pick is left exactly as-is.
  useEffect(
    () =>
      subscribeProbedDevice(() => {
        if (!modelIsDefault.current) return;
        const recommended = recommendedDefaultModel();
        if (settingsRef.current.model === recommended) return;
        // Not routed through updateSettings, so it schedules no write: this
        // is a guess being corrected rather than a user edit, and persisting
        // it would mint an Unnamed Project row on launch for an app nobody
        // has touched yet. If the row does already exist, the correction
        // reaches the store with the user's next settings change.
        applySettings({ ...settingsRef.current, model: recommended });
      }),
    [],
  );

  // Auto-load on startup: the project the user most recently touched (see
  // set_last_project_id, called on every save/load below). Two ways of
  // having no project to restore look alike here and deliberately are not
  // (issue 15):
  //
  // - No "last" pointer at all — a store that predates the pointer, e.g.
  //   first launch after an upgrade. Nothing was ever chosen, so the
  //   most-recently-updated named project is the best guess available.
  // - A pointer to a project that no longer exists. Only discard and the
  //   bar's delete remove rows, and both leave the app on a blank slate —
  //   so the blank slate is what was last touched (map constraint 7), and
  //   launch restores it by loading nothing. Falling back to a named
  //   project here would resurrect exactly what the user just discarded
  //   their way out of.
  //
  // The dead pointer is never rewritten here: it is the durable record
  // that the last touch was a deletion, and the next load, import, or
  // save overwrites it anyway. (The no-pointer branch does end up writing
  // one, via the load it triggers.)
  //
  // Runs once on mount only; a project explicitly loaded/created/deleted
  // afterward should never be silently overridden by this.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const lastId = await projectApi.getLastProjectId();
        if (cancelled) return;
        if (lastId != null) {
          try {
            const project = await projectApi.getProject(lastId);
            if (!cancelled) applyLoaded(project);
          } catch {
            // Dead pointer: the project was deleted, and the deletion left
            // a blank slate. Restore it by loading nothing.
          }
          return;
        }
        const projects = await projectApi.listProjects();
        if (cancelled || projects.length === 0) return;
        loadMutation.mutate(projects[0].id);
      } catch {
        // Best-effort — if something's wrong with the local store, the
        // project bar's empty state already communicates that.
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const value: ProjectContextValue = {
    projectId,
    projectTag,
    projectName,
    settings,
    setSettings: updateSettings,
    decklistText,
    cards,
    importDecklistText: async (text: string) => {
      await importDecklistMutation.mutateAsync(text);
    },
    importingDecklistText: importDecklistMutation.isPending,
    importResolvedCards: async (text, resolvedCards) => {
      await importResolvedCardsMutation.mutateAsync({ text, cards: resolvedCards });
    },
    importingResolvedCards: importResolvedCardsMutation.isPending,
    removeCard: (cardId: number) => removeCardMutation.mutate(cardId),
    setCardQuantity: (cardId: number, quantity: number) =>
      setCardQuantityMutation.mutate({ cardId, quantity }),
    setCardPrinting: async (cardId, printing) => {
      await setCardPrintingMutation.mutateAsync({ cardId, printing });
    },
    applyCardResolutions: async (updates) => {
      await applyCardResolutionsMutation.mutateAsync(updates);
    },
    isNamed: projectName.trim().length > 0,
    rename: async (name: string) => {
      await renameMutation.mutateAsync(name);
    },
    saveAs: (name: string) => saveAsMutation.mutate(name),
    flushPendingWrites: async () => {
      flushSettingsWrite();
      // The chain, not just the write this scheduled: an edit from a
      // moment earlier may still be in flight, and it is the same store.
      await settingsWriteChain.current;
      // After the settings write rather than alongside it: from a project
      // with no row yet, both of them call ensureProjectRow, and run
      // together they would both find projectIdRef still null and ask for
      // the row twice. The store settles that race by itself
      // (project_store.rs::get_or_create_unnamed_project_id), so this is
      // ordering rather than safety — sequentially the first adopts the id
      // and the second simply finds it.
      try {
        await nameCommitFlush.current?.();
      } catch {
        // The registered flush is supposed to swallow its own failures
        // (ProjectBar renders a collision beside the field). Belt and
        // braces, because the caller is the quit path: a throw here would
        // cost invokeAnswerQuitPrompt its turn and leave the app hanging
        // until Rust's timeout gives up on it.
      }
    },
    registerNameCommitFlush: (flush) => {
      nameCommitFlush.current = flush;
    },
    createNew,
    // Deliberately the state-read id, not projectIdRef: this is what the
    // bar renders its button against, and it must agree with what the user
    // is looking at.
    newWouldDiscard: isDiscard(projectId) && cards.length > 0,
    load: (id: number) => loadMutation.mutate(id),
    remove: (id: number) => deleteMutation.mutate(id),
    error,
  };

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}
