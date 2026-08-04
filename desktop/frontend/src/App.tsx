import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import ProjectBar from "./components/ProjectBar";
import ServerStatusToast from "./components/ServerStatusToast";
import { useConnection } from "./connection";
import { ProjectProvider } from "./context/ProjectContext";
import DecklistPage from "./pages/DecklistPage";
import PdfPage from "./pages/PdfPage";
import TasksPage from "./pages/TasksPage";

// ProjectBar renders above the routed tabs and stays mounted across
// navigation — deliberately not a route itself, matching
// ui/projects.py::render_project_bar's old "always visible, not a tab"
// placement.
export default function App() {
  const { sessionKey } = useConnection();

  // Remounting on a connection switch is load-bearing, not just tidy:
  // ProjectContext's auto-load-latest effect has an empty dep array and
  // runs on mount only, and its default settings read the connection mode
  // once at init. Without this the new server's projects never load and
  // the model default stays on the old mode's. Keyed here rather than
  // higher up so the user stays on the tab they switched from.
  return (
    <ProjectProvider key={sessionKey}>
      <div className="app">
        <ServerStatusToast />
        <ProjectBar />
        {/* NavLink applies an `active` class on the matched route by
            default — .tabs styles the underline off that, no manual
            location matching needed. */}
        <nav className="tabs">
          <NavLink to="/decklist">Decklist</NavLink>
          <NavLink to="/pdf">PDF</NavLink>
          <NavLink to="/tasks">Tasks</NavLink>
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
