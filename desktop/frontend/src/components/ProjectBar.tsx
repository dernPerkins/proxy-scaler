import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useProject } from "../context/ProjectContext";
import { useServerReadiness } from "../config";

// Always-visible, not a tab — matches ui/projects.py::render_project_bar's
// old placement above the Decklist/PDF tabs.
export default function ProjectBar() {
  const project = useProject();
  const readiness = useServerReadiness();
  const projectsQuery = useQuery({ queryKey: ["projects"], queryFn: () => api.listProjects() });
  const [saveAsName, setSaveAsName] = useState("");
  const [showSaveAs, setShowSaveAs] = useState(false);
  const [selectedLoadId, setSelectedLoadId] = useState<number | "">("");

  const projects = projectsQuery.data ?? [];

  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        alignItems: "center",
        padding: "8px 0",
        marginBottom: 16,
        borderBottom: "1px solid #444",
        flexWrap: "wrap",
      }}
    >
      <span>{project.isSaved ? `saved · #${project.projectId}` : "unsaved"}</span>
      <input
        value={project.projectName}
        onChange={(e) => project.setProjectName(e.target.value)}
        placeholder="Project name"
      />
      <button onClick={project.save} disabled={project.saving}>
        Save
      </button>
      <button onClick={project.createNew}>New</button>
      <button onClick={() => setShowSaveAs((v) => !v)}>Save As…</button>
      {showSaveAs && (
        <span>
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
        </span>
      )}
      {projects.length > 0 ? (
        <>
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
            onClick={() => {
              if (selectedLoadId !== "" && confirm("Permanently delete this project from the database?")) {
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
        <span>Starting local server…</span>
      ) : (
        <span>No saved projects yet — enter a name and click Save.</span>
      )}
      {project.error && <span style={{ color: "#c66" }}>{project.error}</span>}
    </div>
  );
}
