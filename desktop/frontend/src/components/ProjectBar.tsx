import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { projectApi } from "../api/project";
import { useProject } from "../context/ProjectContext";
import { useServerReadiness } from "../config";

// Always-visible, not a tab — matches ui/projects.py::render_project_bar's
// old placement above the Decklist/PDF tabs.
export default function ProjectBar() {
  const project = useProject();
  const readiness = useServerReadiness();
  const projectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: () => projectApi.listProjects(),
  });
  const [saveAsName, setSaveAsName] = useState("");
  const [showSaveAs, setShowSaveAs] = useState(false);
  const [selectedLoadId, setSelectedLoadId] = useState<number | "">("");

  const projects = projectsQuery.data ?? [];

  return (
    <div className="project-bar panel">
      <span className={project.isSaved ? "chip chip-saved" : "chip"}>
        {project.isSaved ? `Saved · #${project.projectId}` : "Unsaved"}
      </span>

      <input
        className="grow"
        value={project.projectName}
        onChange={(e) => project.setProjectName(e.target.value)}
        placeholder="Project name"
      />

      <button className="btn-primary" onClick={project.save} disabled={project.saving}>
        {project.saving ? "Saving…" : "Save"}
      </button>
      <button onClick={project.createNew}>New</button>
      <button onClick={() => setShowSaveAs((v) => !v)}>Save As…</button>

      {showSaveAs && (
        <>
          <input
            value={saveAsName}
            onChange={(e) => setSaveAsName(e.target.value)}
            placeholder="New project name"
          />
          <button
            onClick={() => {
              if (saveAsName.trim()) {
                project.saveAs(saveAsName.trim());
                setSaveAsName("");
                setShowSaveAs(false);
              }
            }}
          >
            Save copy
          </button>
        </>
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
        <span className="hint">No saved projects yet — enter a name and click Save.</span>
      )}

      {project.error && <span className="error-text">{project.error}</span>}
    </div>
  );
}
