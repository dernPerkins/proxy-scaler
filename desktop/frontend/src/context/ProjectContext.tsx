import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { projectApi } from "../api/project";
import { getConnectionMode, getProbedDevice, subscribeProbedDevice } from "../config";
import type { CardRow, LoadedProject, ProjectSettings } from "../api/project";

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
  };
}

interface ProjectContextValue {
  projectId: number | null;
  /** Opaque tag passed to the generation server to scope tasks/gallery —
   *  see ARCHITECTURE.md. Null until the project has been saved once. */
  projectTag: string | null;
  projectName: string;
  settings: ProjectSettings;
  setSettings: (updater: ProjectSettings | ((s: ProjectSettings) => ProjectSettings)) => void;
  setProjectName: (name: string) => void;
  decklistText: string;
  cards: CardRow[];
  /** Parses `text` and adds any new cards to `cards` — additive, never
   *  removes an existing card (see project_store.rs::import_decklist_text).
   *  Also remembers `text` itself as decklistText, purely as a "what did I
   *  last paste" convenience. */
  importDecklistText: (text: string) => void;
  importingDecklistText: boolean;
  removeCard: (cardId: number) => void;
  isSaved: boolean;
  save: () => void;
  /** Awaitable save, for callers that must not proceed until it lands
   *  (the connection switcher saves before resetting the whole UI).
   *  Rejects on failure so the caller can abort. */
  saveAsync: () => Promise<void>;
  saveAs: (name: string) => void;
  createNew: () => void;
  load: (id: number) => void;
  remove: (id: number) => void;
  saving: boolean;
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

  // Exposed instead of the raw setter so the model can't be quietly
  // overwritten after the user has expressed a preference. Every other
  // setting passes through untouched.
  function updateSettings(
    updater: ProjectSettings | ((s: ProjectSettings) => ProjectSettings),
  ) {
    setSettings((prev) => {
      const next = typeof updater === "function" ? updater(prev) : updater;
      if (next.model !== prev.model) modelIsDefault.current = false;
      return next;
    });
  }

  function applyLoaded(project: LoadedProject) {
    setProjectId(project.id);
    setProjectTag(project.tag);
    setProjectName(project.name);
    setSettings(project.settings);
    // A saved project's stored model is an explicit choice, whatever it
    // happens to be — never second-guess it when the probe lands.
    modelIsDefault.current = false;
    setDecklistTextState(project.import_decklist_text);
    setCards(project.cards);
    setError(null);
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      const name = projectName.trim();
      if (!name) throw new Error("Enter a project name before saving.");
      const summary =
        projectId != null
          ? await projectApi.updateProject(projectId, name, settings)
          : await projectApi.createProject(name);
      // create_project only takes a name — push this session's current
      // settings immediately after, so a brand-new project doesn't lose
      // whatever the user already configured in the sidebar before ever
      // clicking Save.
      if (projectId == null) {
        await projectApi.updateProject(summary.id, summary.name, settings);
      }
      await projectApi.setLastProjectId(summary.id);
      return summary;
    },
    onSuccess: (project) => {
      setProjectId(project.id);
      setProjectTag(project.tag);
      setProjectName(project.name);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const saveAsMutation = useMutation({
    mutationFn: async (name: string) => {
      const trimmed = name.trim();
      if (!trimmed) throw new Error("Name required.");
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
        setProjectId(null);
        setProjectTag(null);
        setProjectName("");
        setDecklistTextState("");
        setCards([]);
      }
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const importDecklistMutation = useMutation({
    mutationFn: async (text: string) => {
      if (projectId == null) throw new Error("Save the project before importing a decklist.");
      const newCards = await projectApi.importDecklistText(projectId, text);
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

  function createNew() {
    setProjectId(null);
    setProjectTag(null);
    setProjectName("");
    setSettings(getDefaultSettings());
    modelIsDefault.current = true;
    setDecklistTextState("");
    setCards([]);
    setError(null);
  }

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
        setSettings((s) => {
          const recommended = recommendedDefaultModel();
          return s.model === recommended ? s : { ...s, model: recommended };
        });
      }),
    [],
  );

  // Auto-load on startup: the project the user most recently touched (see
  // set_last_project_id, called on every save/load below), falling back
  // to the most-recently-updated project if there's no "last" pointer yet
  // (e.g. first launch after an upgrade) or it points at a project that's
  // since been deleted. Runs once on mount only; a project explicitly
  // loaded/created/deleted afterward should never be silently overridden
  // by this.
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
            return;
          } catch {
            // Stale pointer (project deleted) — fall through to latest.
          }
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
    setProjectName,
    decklistText,
    cards,
    importDecklistText: (text: string) => importDecklistMutation.mutate(text),
    importingDecklistText: importDecklistMutation.isPending,
    removeCard: (cardId: number) => removeCardMutation.mutate(cardId),
    isSaved: projectId != null,
    save: () => saveMutation.mutate(),
    saveAsync: async () => {
      await saveMutation.mutateAsync();
    },
    saveAs: (name: string) => saveAsMutation.mutate(name),
    createNew,
    load: (id: number) => loadMutation.mutate(id),
    remove: (id: number) => deleteMutation.mutate(id),
    saving: saveMutation.isPending || saveAsMutation.isPending,
    error,
  };

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}
