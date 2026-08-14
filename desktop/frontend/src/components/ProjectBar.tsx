import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { projectApi } from "../api/project";
import { useProject } from "../context/ProjectContext";
import { useServerReadiness } from "../config";

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
  const [selectedLoadId, setSelectedLoadId] = useState<number | "">("");

  // The name field is the whole naming mechanism: there is no Save button,
  // so what is typed lives here until a pause (or Enter) commits it. The
  // context holds only the *stored* name.
  const [nameDraft, setNameDraft] = useState(project.projectName);
  const [nameError, setNameError] = useState<string | null>(null);
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const committing = useRef(false);
  const lastProjectId = useRef(project.projectId);

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
    const switched = lastProjectId.current !== project.projectId;
    lastProjectId.current = project.projectId;
    // Naming an app that held no row yet creates one, which moves the id
    // without being a switch at all — and the stored name is still '' at
    // that point, so adopting it would wipe the name being committed.
    if (committing.current || !switched) return;
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

  function closeDuplicateField() {
    setShowDuplicate(false);
    setDuplicateName("");
  }

  function cancelPendingCommit() {
    if (commitTimer.current != null) {
      clearTimeout(commitTimer.current);
      commitTimer.current = null;
    }
  }

  // Restarted per keystroke, so a name is written once, when typing stops.
  function scheduleCommit(name: string) {
    cancelPendingCommit();
    commitTimer.current = setTimeout(() => void commitName(name), NAME_COMMIT_DEBOUNCE_MS);
  }

  async function commitName(name: string) {
    cancelPendingCommit();
    const trimmed = name.trim();
    // Clearing the field is ignored rather than treated as un-naming
    // (.scratch/optional-projects/spec.md §5.4): the UPDATE to '' would
    // violate UNIQUE (project_store.rs:22) whenever an Unnamed Project
    // exists, and Delete is the gesture for getting rid of a project. Blur
    // puts the stored name back.
    if (!trimmed || trimmed === project.projectName) return;
    committing.current = true;
    try {
      await project.rename(trimmed);
      setNameError(null);
    } catch (err) {
      // Only ever after a settle, never mid-keystroke — this is the point
      // of the debounce. Typing toward "Krenko Goblins" would otherwise
      // collide on "Krenko" on the way past. The typed text stays; nothing
      // was committed.
      setNameError(err instanceof Error ? err.message : String(err));
    } finally {
      committing.current = false;
    }
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
  // "Nothing yet" for the empty slate — prototype variant A, the layout
  // chosen in .scratch/optional-projects/decisions/05-projectbar-prototype.md.
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
          // New goes first now that it can ask before discarding: declining
          // that confirm has to change nothing, this field included. Being
          // synchronous, it can't be interrupted by a queued commit firing
          // — and a commit already *in flight* is handled at createNew,
          // which treats a renaming row as named rather than deleting it.
          if (!project.createNew()) return;
          // A queued commit belongs to the slate being discarded: left
          // alone it would name it half a second later, and from a project
          // with no row yet nothing else here would notice New at all.
          cancelPendingCommit();
          setNameDraft("");
          setNameError(null);
          closeDuplicateField();
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
          <select
            value={selectedLoadId}
            onChange={(e) => setSelectedLoadId(e.target.value ? Number(e.target.value) : "")}
          >
            <option value="">Load project…</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} (#{p.id})
              </option>
            ))}
          </select>
          <button
            onClick={() => selectedLoadId !== "" && project.load(selectedLoadId)}
            disabled={selectedLoadId === ""}
          >
            Load
          </button>
          <button
            className="btn-danger"
            onClick={() => {
              if (
                selectedLoadId !== "" &&
                confirm("Permanently delete this project from the database?")
              ) {
                project.remove(selectedLoadId);
                setSelectedLoadId("");
              }
            }}
            disabled={selectedLoadId === ""}
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
    </div>
  );
}
