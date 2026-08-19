import { useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import ConnectionLostDialog from "./components/ConnectionLostDialog";
import DownloadProgressModal from "./components/DownloadProgressModal";
import ProjectBar from "./components/ProjectBar";
import QuitPrompt from "./components/QuitPrompt";
import ServerStatusToast from "./components/ServerStatusToast";
import UpdatePrompt from "./components/UpdatePrompt";
import VersionMismatchToast from "./components/VersionMismatchToast";
import { ProjectProvider } from "./context/ProjectContext";
import DecklistPage from "./pages/DecklistPage";
import PdfPage from "./pages/PdfPage";
import TasksPage from "./pages/TasksPage";
import { isTauri } from "./tauri";
import { getAppVersion } from "./update";

// The app's own version, shown at the far end of the tab bar — the one
// always-visible, out-of-the-way spot — so "what version am I on?" never
// requires an update check or a trip to the OS's app manager. Empty in a
// plain browser dev tab (no Tauri, no version to ask for).
function AppVersion() {
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    if (!isTauri()) return;
    getAppVersion()
      .then(setVersion)
      .catch(() => {});
  }, []);
  if (!version) return null;
  return <span className="tabs-version">v{version}</span>;
}

// ProjectBar renders above the routed tabs and stays mounted across
// navigation — deliberately not a route itself, matching
// ui/projects.py::render_project_bar's old "always visible, not a tab"
// placement.
//
// ProjectProvider no longer remounts on a Local/Remote switch (see
// connection.tsx::switchTo) — project data is local-only now and
// genuinely unaffected by which generation server is connected, so
// there's nothing about it that a connection change needs to reset.
export default function App() {
  return (
    <ProjectProvider>
      <div className="app">
        <ServerStatusToast />
        <VersionMismatchToast />
        <ConnectionLostDialog />
        <DownloadProgressModal />
        <UpdatePrompt />
        {/* Mounted for its close-request listener, not for what it draws:
            it renders nothing until the window is actually closing. */}
        <QuitPrompt />
        <ProjectBar />
        {/* NavLink applies an `active` class on the matched route by
            default — .tabs styles the underline off that, no manual
            location matching needed. */}
        <nav className="tabs">
          <NavLink to="/decklist">Decklist</NavLink>
          <NavLink to="/pdf">PDF</NavLink>
          <NavLink to="/tasks">Tasks</NavLink>
          <AppVersion />
        </nav>
        <Routes>
          <Route path="/" element={<Navigate to="/decklist" replace />} />
          <Route path="/decklist" element={<DecklistPage />} />
          <Route path="/pdf" element={<PdfPage />} />
          <Route path="/tasks" element={<TasksPage />} />
        </Routes>
      </div>
    </ProjectProvider>
  );
}
