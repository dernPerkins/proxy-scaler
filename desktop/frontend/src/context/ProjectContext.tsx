import { createContext, useContext, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ProjectSettings } from "../api/types";

const DEFAULT_SETTINGS: ProjectSettings = {
  model: "ultrasharp_v2",
  dpi_targets: [800],
  page_size: 6,
  skip_existing: true,
  output_dir: "output",
  cache_dir: "imgcache",
  weights_dir: "weights",
  tile_size: 0,
};

interface ProjectContextValue {
  projectId: number | null;
  projectName: string;
  settings: ProjectSettings;
  setSettings: (updater: ProjectSettings | ((s: ProjectSettings) => ProjectSettings)) => void;
  setProjectName: (name: string) => void;
  isSaved: boolean;
  save: () => void;
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
  const [projectName, setProjectName] = useState("");
  const [settings, setSettings] = useState<ProjectSettings>(DEFAULT_SETTINGS);
  const [error, setError] = useState<string | null>(null);

  const saveMutation = useMutation({
    mutationFn: async () => {
      const name = projectName.trim();
      if (!name) throw new Error("Enter a project name before saving.");
      return projectId != null
        ? api.updateProject(projectId, name, settings)
        : api.createProject(name, settings);
    },
    onSuccess: (project) => {
      setProjectId(project.id);
      setProjectName(project.name);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const saveAsMutation = useMutation({
    mutationFn: async (name: string) => {
      if (!name.trim()) throw new Error("Name required.");
      return api.createProject(name.trim(), settings);
    },
    onSuccess: (project) => {
      setProjectId(project.id);
      setProjectName(project.name);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const loadMutation = useMutation({
    mutationFn: (id: number) => api.getProject(id),
    onSuccess: (project) => {
      setProjectId(project.id);
      setProjectName(project.name);
      setSettings(project.settings);
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["cards", project.id] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteProject(id),
    onSuccess: (_data, id) => {
      if (projectId === id) {
        setProjectId(null);
        setProjectName("");
      }
      queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  function createNew() {
    setProjectId(null);
    setProjectName("");
    setSettings(DEFAULT_SETTINGS);
    setError(null);
  }

  const value: ProjectContextValue = {
    projectId,
    projectName,
    settings,
    setSettings,
    setProjectName,
    isSaved: projectId != null,
    save: () => saveMutation.mutate(),
    saveAs: (name: string) => saveAsMutation.mutate(name),
    createNew,
    load: (id: number) => loadMutation.mutate(id),
    remove: (id: number) => deleteMutation.mutate(id),
    saving: saveMutation.isPending || saveAsMutation.isPending,
    error,
  };

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}
