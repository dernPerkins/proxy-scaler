import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { projectApi } from "../api/project";
import { getConnectionMode } from "../config";
import type { CardRow, LoadedProject, ProjectSettings } from "../api/project";

// Local runs upscaling on-device, so a fast/light model is the sensible
// default; a remote server is assumed to have real GPU headroom, so it
// defaults to the higher-quality (slower) model instead.
function defaultModelForMode(): string {
  return getConnectionMode() === "local" ? "realesrgan_anime_fast" : "ultrasharp_v2";
}

function getDefaultSettings(): ProjectSettings {
  return {
    model: defaultModelForMode(),
    dpi_targets: [1200],
    skip_existing: true,
    tile_size: 0,
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

  function applyLoaded(project: LoadedProject) {
    setProjectId(project.id);
    setProjectTag(project.tag);
    setProjectName(project.name);
    setSettings(project.settings);
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
    setDecklistTextState("");
    setCards([]);
    setError(null);
  }

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
    setSettings,
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
