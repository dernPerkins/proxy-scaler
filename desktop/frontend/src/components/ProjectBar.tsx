import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { projectApi } from "../api/project";
import { useProject } from "../context/ProjectContext";
import { useServerReadiness } from "../config";
import ConfirmDialog from "./ConfirmDialog";

// Long enough that typing "Krenko Goblins" is one commit rather than two,
// short enough that the chip flips while the user is still looking at the
// field — with no Save button, that flip is the only confirmation the name
// landed (.scratch/optional-projects/decisions/08-saved-unsaved-vocabulary.md).
const NAME_COMMIT_DEBOUNCE_MS = 500;

// Always-visible, not a tab — matches ui/projects.py::render_project_bar's
// old placement above the Decklist/PDF tabs.
export default function ProjectBar() {
  const project = useProject();
  const readiness = useServerReadiness();
  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: () => projectApi.listProjects(),
  });
  const [duplicateName, setDuplicateName] = useState("");
  const [showDuplicate, setShowDuplicate] = useState(false);
  // The two questions this bar asks before destroying something. Both were
  // `window.confirm` until issue 16: native dialogs never appear inside
  // Tauri's WKWebView, so on macOS both gestures went ahead unasked.
  const [confirmingNew, setConfirmingNew] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  // The name field is the whole naming mechanism: there is no Save button,
  // so what is typed lives here until a pause (or Enter) commits it. The
  // context holds only the *stored* name.
  const [nameDraft, setNameDraft] = useState(project.projectName);
  const [nameError, setNameError] = useState<string | null>(null);
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // What that timer is holding, so something other than the timer can land
  // it — see flushPendingNameCommit.
  const queuedName = useRef<string | null>(null);
  // Commits run one after another rather than overlapping, and the chain is
  // what "the name has landed" means to a caller waiting on it. Never
  // rejects: commitName renders its own failures beside the field.
  const commitChain = useRef<Promise<void>>(Promise.resolve());
  // A count rather than a flag, because commits can now queue behind one
  // another: the one in front finishing must not clear the state the
  // draft-reset effect reads while a second is still to run.
  const inFlightCommits = useRef(0);
  const lastProjectId = useRef(project.projectId);
  // The queued name the discard confirm is holding, if there was one when
  // New was clicked. The old native confirm blocked the whole webview, so
  // no timer could fire under it; a React modal stays up for as long as a
  // person takes to read it, which is many times the 500ms debounce. Left
  // running, the queued commit would name the very slate the dialog is
  // asking permission to discard. So it is cancelled while the question is
  // open and put back if the answer is No — declining changes nothing, and
  // a name half-typed into the field is part of that nothing.
  const suspendedCommit = useRef<string | null>(null);

  // Load, Delete and Duplicate all swap the project out from under a field
  // that may be holding text typed for the outgoing one, so the draft
  // follows them. Keyed on the id rather than on the stored name: the name
  // changing on its own is this field's own commit landing, and adopting it
  // would pull the field back to text the user may already have typed past.
  //
  // New is handled at its button instead — from a project that has no row
  // yet the id is null on both sides of it, so there is no change here to
  // notice.
  useEffect(() => {
    const previousId = lastProjectId.current;
    const switched = previousId !== project.projectId;
    lastProjectId.current = project.projectId;
    // Naming an app that held no row yet creates one, which moves the id
    // without being a switch at all — and the stored name is still '' at
    // that point, so adopting it would wipe the name being committed.
    if (inFlightCommits.current > 0 || !switched) return;
    // A commit in flight only covers the row being born from *this field's*
    // own commit. Any write path can create it — an import, a slider drag, the
    // startup restore — and gaining a first row is never a switch either:
    // there is no outgoing project to follow. Without this, typing a name
    // and then clicking Import within the debounce cancels the queued
    // commit and blanks the field, losing the name silently.
    //
    // An untouched field still takes what arrives, which is how a restored
    // project's name reaches it (for a newly born row that name is '',
    // so this is the same no-op either way).
    if (previousId == null && nameDraft.trim()) return;
    cancelPendingCommit();
    setNameDraft(project.projectName);
    setNameError(null);
    // The Duplicate field was holding a name for the outgoing project, and
    // the disclosure itself is hidden while unnamed — left open it would
    // spring back with stale text on the next named project.
    closeDuplicateField();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.projectId, project.projectName]);

  // A commit still queued when the bar goes away has nowhere to land.
  useEffect(() => cancelPendingCommit, []);

  // The context holds the stored name and knows nothing of this field's
  // timer, so hand it a way to land one: flushPendingWrites means *all*
  // pending writes, and the quit path awaits it before releasing a teardown
  // that ends in std::process::exit
  // (.scratch/optional-projects/issues/14-flush-the-name-debounce-at-quit.md).
  //
  // Registered once, but called through a ref so the flush that runs is the
  // current render's — it compares the draft against today's stored name,
  // not the one this component mounted with.
  const flushLatest = useRef(flushPendingNameCommit);
  useEffect(() => {
    flushLatest.current = flushPendingNameCommit;
  });
  useEffect(() => {
    project.registerNameCommitFlush(() => flushLatest.current());
    return () => project.registerNameCommitFlush(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function closeDuplicateField() {
    setShowDuplicate(false);
    setDuplicateName("");
  }

  function cancelPendingCommit() {
    if (commitTimer.current != null) {
      clearTimeout(commitTimer.current);
      commitTimer.current = null;
    }
    queuedName.current = null;
  }

  // Restarted per keystroke, so a name is written once, when typing stops.
  function scheduleCommit(name: string) {
    cancelPendingCommit();
    queuedName.current = name;
    commitTimer.current = setTimeout(() => void commitName(name), NAME_COMMIT_DEBOUNCE_MS);
  }

  function commitName(name: string): Promise<void> {
    cancelPendingCommit();
    const trimmed = name.trim();
    // Clearing the field is ignored rather than treated as un-naming
    // (.scratch/optional-projects/spec.md §5.4): the UPDATE to '' would
    // violate UNIQUE (project_store.rs:22) whenever an Unnamed Project
    // exists, and Delete is the gesture for getting rid of a project. Blur
    // puts the stored name back.
    if (!trimmed || trimmed === project.projectName) return commitChain.current;
    // Counted up here, synchronously, rather than inside the chained
    // callback: the draft-reset effect reads it to tell "the row is being
    // born from this field's own commit" from a project switch, and the
    // rename that creates the row can be under way before the callback
    // ahead of it in the chain has finished.
    inFlightCommits.current += 1;
    commitChain.current = commitChain.current.then(async () => {
      try {
        await project.rename(trimmed);
        setNameError(null);
      } catch (err) {
        // Only ever after a settle, never mid-keystroke — this is the point
        // of the debounce. Typing toward "Krenko Goblins" would otherwise
        // collide on "Krenko" on the way past. The typed text stays; nothing
        // was committed.
        //
        // Swallowed rather than rethrown, which is also what lets the quit
        // path await this: a collision landing as the app closes keeps the
        // stored name and lets the quit go ahead, because the alternative
        // is refusing to close over a message the user is given no chance
        // to read
        // (.scratch/optional-projects/decisions/08-saved-unsaved-vocabulary.md,
        // amendment).
        setNameError(err instanceof Error ? err.message : String(err));
      } finally {
        inFlightCommits.current -= 1;
      }
    });
    return commitChain.current;
  }

  // Lands whatever the field has queued, for a caller that needs the name
  // in the store before it does something irreversible — the quit path,
  // which awaits this through the context's flushPendingWrites before it
  // releases Rust's teardown, and the pagehide listener beside it. Resolves
  // once the queued commit *and* anything already in flight ahead of it
  // have finished; resolves immediately when the field is idle.
  //
  // suspendedCommit counts as queued: while the discard confirm is up the
  // name is parked there rather than in a timer (see suspendedCommit), and
  // "every pending write" has to mean that one too — the window's X is
  // native chrome outside the webview, so it reaches this even with the
  // modal open, and ticket 14's whole point is that the quit path finds
  // nothing left behind. The discard itself is unaffected: it was never
  // answered, so it does not happen, and the project quits named.
  //
  // Cleared on the way past so a Cancel arriving afterwards cannot
  // reschedule a name that has already landed.
  function flushPendingNameCommit(): Promise<void> {
    const queued = queuedName.current ?? suspendedCommit.current;
    suspendedCommit.current = null;
    if (queued != null) return commitName(queued);
    return commitChain.current;
  }

  // New, once there is nothing left to ask — either because this New is a
  // detach, or because the confirm came back Yes.
  //
  // createNew goes first, and being synchronous it cannot be interrupted by
  // a queued commit firing between the discard and the field being cleared
  // behind it. A commit already *in flight* is handled inside createNew,
  // which treats a renaming row as named rather than deleting it.
  function applyNew() {
    project.createNew();
    // A queued commit belongs to the slate being discarded: left alone it
    // would name it half a second later, and from a project with no row yet
    // nothing else here would notice New at all. Already cancelled on the
    // confirm path, where the wait itself is the hazard (see
    // suspendedCommit); this covers the New that asked nothing.
    suspendedCommit.current = null;
    cancelPendingCommit();
    setNameDraft("");
    setNameError(null);
    closeDuplicateField();
  }

  // Declining has to leave the field exactly as the click found it, timer
  // included — so the suspended commit is rescheduled rather than dropped.
  // It restarts the 500ms, which is the honest reading: the pause the user
  // was in the middle of was interrupted by their own detour.
  function cancelNew() {
    setConfirmingNew(false);
    const queued = suspendedCommit.current;
    suspendedCommit.current = null;
    if (queued != null) scheduleCommit(queued);
  }

  const projects = projectsQuery.data ?? [];
  // The Decklist tab counts rows, not copies ("Cards (N)",
  // DecklistPage.tsx:443) — the chip says the same N.
  const cardCount = project.cards.length;

  // Nothing here says "unsaved", because nothing is
  // (.scratch/optional-projects/spec.md §5.5). What the chip reports is
  // whether the project can be found again: its name once it has one, and
  // what is sitting in the Unnamed Project until then. With no Save button,
  // watching this flip to the name is the only confirmation the debounce
  // fired.
  //
  // "Nothing yet" for the empty slate — the chip's three states as settled
  // in .scratch/optional-projects/issues/07-the-projectbar.md.
  // The `#{id}` fallback is unreachable while isNamed means a non-empty
  // stored name; it is there so the chip can never render blank.
  const chipLabel = project.isNamed
    ? project.projectName || `#${project.projectId}`
    : cardCount === 0
      ? "Nothing yet"
      : `Unnamed · ${cardCount} ${cardCount === 1 ? "card" : "cards"}`;

  return (
    <div className="project-bar panel">
      <span className={project.isNamed ? "chip chip-named" : "chip"}>{chipLabel}</span>

      <input
        className="grow"
        value={nameDraft}
        onChange={(e) => {
          setNameDraft(e.target.value);
          // The error described text the user has now moved past.
          setNameError(null);
          scheduleCommit(e.target.value);
        }}
        onKeyDown={(e) => {
          // Enter skips the wait: waiting 500ms after deciding feels broken.
          if (e.key === "Enter") void commitName(nameDraft);
        }}
        onBlur={() => {
          if (nameDraft.trim()) return;
          cancelPendingCommit();
          setNameDraft(project.projectName);
          setNameError(null);
        }}
        placeholder="Project name"
      />
      {nameError && <span className="error-text">{nameError}</span>}

      <button
        onClick={() => {
          // Spec §5.6: an Unnamed Project holding cards is wiped behind a
          // confirm. Everything else is a detach and goes straight through.
          if (project.newWouldDiscard) {
            suspendedCommit.current = queuedName.current;
            cancelPendingCommit();
            setConfirmingNew(true);
            return;
          }
          applyNew();
        }}
      >
        New
      </button>

      {/* Named Projects only, and hidden rather than disabled: from an
          Unnamed Project this would do what typing a name already does
          (.scratch/optional-projects/spec.md §5.5), so there is nothing
          here to explain or re-enable. */}
      {project.isNamed && (
        <>
          <button onClick={() => setShowDuplicate((v) => !v)}>Duplicate…</button>
          {showDuplicate && (
            <>
              <input
                value={duplicateName}
                onChange={(e) => setDuplicateName(e.target.value)}
                placeholder="Name for the copy"
              />
              <button
                onClick={() => {
                  if (duplicateName.trim()) {
                    project.saveAs(duplicateName.trim());
                    closeDuplicateField();
                  }
                }}
              >
                Duplicate
              </button>
            </>
          )}
        </>
      )}

      {/* A nudge, not a demand — and specifically not "name it to keep it":
          it is kept either way. Naming is what makes it findable again. */}
      {!project.isNamed && cardCount > 0 && (
        <span className="hint">Name it to find it again later.</span>
      )}

      <span className="spacer" />

      {projects.length > 0 ? (
        <>
          <div className="divider-v" />
          {/* A menu, not a selection: picking a project loads it on the
              spot (every edit is already saved, so there is nothing a
              separate Load click would be confirming) and the value stays
              on the placeholder. Delete therefore acts on the *loaded*
              project — the only "this project" left once selecting means
              loading — and stays off for the unnamed slate, whose way out
              is the New button's discard flow, not deletion. */}
          <select
            value=""
            onChange={(e) => {
              if (e.target.value) project.load(Number(e.target.value));
            }}
          >
            <option value="">Load project…</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} (#{p.id})
              </option>
            ))}
          </select>
          <button
            className="btn-danger"
            onClick={() => setConfirmingDelete(true)}
            disabled={project.projectId == null || !project.isNamed}
          >
            Delete
          </button>
        </>
      ) : readiness.status === "starting" ? (
        <span className="hint">Starting local server…</span>
      ) : (
        <span className="hint">No named projects yet — name one and it appears here.</span>
      )}

      {project.error && <span className="error-text">{project.error}</span>}

      {/* The prompt is new, and lives only on this branch. Today's silent
          New threw away React state that was never promised to survive;
          since settings and cards began writing through (spec §5.2), the
          same click throws away work the app has been quietly keeping. */}
      {confirmingNew && (
        <ConfirmDialog
          title="Discard this unnamed project?"
          confirmLabel="Discard"
          onCancel={cancelNew}
          onConfirm={() => {
            setConfirmingNew(false);
            applyNew();
          }}
        >
          Its {cardCount} {cardCount === 1 ? "card" : "cards"} and gallery entries are
          removed. This cannot be undone.
        </ConfirmDialog>
      )}

      {/* Names the project, which the native confirm never did — the
          picker loads on selection, so "this project" is the one
          currently loaded — the same project the rest of the bar is
          showing.

          The id test is TypeScript's, not a guard against a stale flag:
          `remove` wants a number and projectId is `number | null`. Both
          answers below clear confirmingDelete, so it cannot outlive its
          dialog. */}
      {confirmingDelete && project.projectId != null && (
        <ConfirmDialog
          title="Delete this project?"
          confirmLabel="Delete"
          onCancel={() => setConfirmingDelete(false)}
          onConfirm={() => {
            setConfirmingDelete(false);
            project.remove(project.projectId as number);
          }}
        >
          {project.projectName || `#${project.projectId}`} is permanently removed from
          the database, cards and settings included. This cannot be undone.
        </ConfirmDialog>
      )}
    </div>
  );
}
