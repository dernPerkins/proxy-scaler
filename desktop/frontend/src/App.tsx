import { useCallback, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import AppSettingsModal from "./components/AppSettingsModal";
import CardDbImportModal from "./components/CardDbImportModal";
import CardDbPrompt from "./components/CardDbPrompt";
import MpcCatalogPrompt from "./components/MpcCatalogPrompt";
import GearIcon from "./components/GearIcon";
import ConnectionLostDialog from "./components/ConnectionLostDialog";
import DownloadProgressModal from "./components/DownloadProgressModal";
import UploadProgressModal from "./components/UploadProgressModal";
import ProjectBar from "./components/ProjectBar";
import QuitPrompt from "./components/QuitPrompt";
import ResumeTasksPrompt from "./components/ResumeTasksPrompt";
import CpuFallbackPrompt from "./components/CpuFallbackPrompt";
import ServerBootModal from "./components/ServerBootModal";
import ToastStack from "./components/ToastStack";
import ServerStatusToast from "./components/ServerStatusToast";
import VersionMismatchToast from "./components/VersionMismatchToast";
import { ProjectProvider } from "./context/ProjectContext";
import DecklistPage from "./pages/DecklistPage";
import CustomsPage from "./pages/CustomsPage";
import MpcFillPage from "./pages/MpcFillPage";
import BacksPage from "./pages/BacksPage";
import ExportPage from "./pages/ExportPage";
import PdfPage from "./pages/PdfPage";
import TasksPage from "./pages/TasksPage";
import { hasShortcutModifier, isDialogOpen, shortcutLabel } from "./shortcuts";
import { isTauri } from "./tauri";
import TutorialButton from "./tutorial/TutorialButton";
import { ROUTE_TOURS } from "./tutorial/tours";
import {
  getAppVersion,
  requestPatchNotesPrompt,
  requestUpdatePrompt,
  useAvailableUpdate,
} from "./update";

// The app's own version, shown at the far end of the tab bar — the one
// always-visible, out-of-the-way spot — so "what version am I on?" never
// requires an update check or a trip to the OS's app manager. Empty in a
// plain browser dev tab (no Tauri, no version to ask for).
//
// When the boot check found an update, an "Update to vX.Y.Z" button sits
// to the label's left. This is the persistent way back to an update the
// user dismissed or skipped — without it, "Skip this version" would leave
// the website as the only path — and it deliberately survives the skip:
// the skip suppresses the automatic boot modal, not the affordance.
// Clicking re-opens the same UpdatePrompt flow (update.ts's store carries
// the signal across the two component trees).
//
// The version label itself is a button too: it reopens the Patch Notes
// dialog over the same cross-tree bridge (PatchNotesPrompt mounts above
// the gate in main.tsx). Styled to stay "present but ignorable" — see
// .tabs-version-btn.
//
// The gear beside it opens App Settings (client-side, app-wide
// preferences); the modal itself is mounted with the other global modals.
function AppVersion({ onOpenSettings }: { onOpenSettings: () => void }) {
  const [version, setVersion] = useState<string | null>(null);
  const update = useAvailableUpdate();
  useEffect(() => {
    if (!isTauri()) return;
    getAppVersion()
      .then(setVersion)
      .catch(() => {});
  }, []);
  return (
    <span className="tabs-version">
      <TabTutorialButton />
      {update && (
        <button className="btn-sm btn-ok" onClick={requestUpdatePrompt}>
          Update to v{update.latest}
        </button>
      )}
      {/* Same slide-open pill as the Tutorial button beside it. */}
      <button
        type="button"
        className="reveal-btn"
        aria-label="App Settings"
        onClick={onOpenSettings}
      >
        <span className="reveal-btn-icon" aria-hidden="true">
          <GearIcon className="reveal-btn-svg" />
        </span>
        <span className="reveal-btn-label">Settings</span>
      </button>
      {version && (
        <button
          className="tabs-version-btn"
          title="Patch notes"
          onClick={requestPatchNotesPrompt}
        >
          v{version}
        </button>
      )}
    </span>
  );
}

// The tab bar, in order. One list feeds both the NavLinks and the
// Ctrl/Cmd+1..N shortcuts, so the number a tooltip promises is always the
// number that gets there.
const TABS = [
  { to: "/decklist", label: "Decklist" },
  { to: "/customs", label: "Customs" },
  { to: "/mpc", label: "MPC Fill" },
  { to: "/backs", label: "Backs" },
  { to: "/pdf", label: "PDF" },
  { to: "/export", label: "ZIP" },
  { to: "/tasks", label: "Tasks" },
];

// Ctrl/Cmd+1..7 jumps to that tab, the same way browsers number theirs.
// Matched on the physical key (e.code) rather than e.key so the top row
// works on layouts where the unshifted digit key types something else
// (AZERTY's & é " …), which is also what browsers do for their own tab
// shortcuts. Digits past the last tab do nothing; Shift is left to the
// browser/webview, whose Ctrl+Shift+digit chords are its own.
function useTabShortcuts() {
  const navigate = useNavigate();
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!hasShortcutModifier(e) || e.shiftKey) return;
      const digit = /^Digit([1-9])$/.exec(e.code);
      if (!digit) return;
      const tab = TABS[Number(digit[1]) - 1];
      if (!tab) return;
      // Claimed even while a dialog is up: the webview would otherwise
      // act on the chord itself (a browser dev tab switches its own tabs).
      e.preventDefault();
      if (isDialogOpen()) return;
      navigate(tab.to);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [navigate]);
}

// The ? that replays the current tab's tour. In the tab bar's right-hand
// cluster because the pages share no header of their own — this is the
// one top-right spot every tab has.
function TabTutorialButton() {
  const { pathname } = useLocation();
  const tour = ROUTE_TOURS[pathname];
  return tour ? <TutorialButton tour={tour} /> : null;
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
  useTabShortcuts();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  return (
    <ProjectProvider>
      <div className="app">
        <ServerStatusToast />
        <VersionMismatchToast />
        <ToastStack />
        {/* Holds the screen while the local sidecar starts, and owns the
            failure when it doesn't. Renders nothing once the server is
            ready — which is always, immediately, in Remote mode. */}
        <ServerBootModal />
        <ConnectionLostDialog />
        <DownloadProgressModal />
        <UploadProgressModal />
        {/* Mounted for its close-request listener, not for what it draws:
            it renders nothing until the window is actually closing. */}
        <QuitPrompt />
        {/* The launch-time resume-or-cancel choice for tasks left over
            found on the connected server — the local flavor holds the
            worker until answered, the remote flavor informs about tasks
            already processing. Renders nothing unless leftovers exist. */}
        <ResumeTasksPrompt />
        {/* Raised the moment the worker's GPU OOMs into the CPU fallback —
            offers to cancel the pending queue before a whole deck grinds
            through on CPU. Renders nothing unless a fallback is pending. */}
        <CpuFallbackPrompt />
        {/* The launch-time card-database offer — renders nothing unless
            the connected server has no imported corpus, and waits its
            turn behind the update and resume-tasks dialogs. */}
        <CardDbPrompt />
        {/* Blocks the whole app while a card-database import runs — a
            half-imported corpus would answer printing lookups
            incompletely. Renders nothing otherwise. */}
        <CardDbImportModal />
        {/* The MPC Fill art catalog offer (after the card-database one),
            and the modal that holds the app while a catalog import runs.
            Renders nothing otherwise. */}
        <MpcCatalogPrompt />
        {settingsOpen && <AppSettingsModal onClose={closeSettings} />}
        <ProjectBar />
        {/* NavLink applies an `active` class on the matched route by
            default — .tabs styles the underline off that, no manual
            location matching needed. */}
        <nav className="tabs" data-tour="tabs">
          {TABS.map((tab, i) => (
            <NavLink key={tab.to} to={tab.to} title={shortcutLabel(String(i + 1))}>
              {tab.label}
            </NavLink>
          ))}
          <AppVersion onOpenSettings={() => setSettingsOpen(true)} />
        </nav>
        <Routes>
          <Route path="/" element={<Navigate to="/decklist" replace />} />
          <Route path="/decklist" element={<DecklistPage />} />
          <Route path="/customs" element={<CustomsPage />} />
          <Route path="/mpc" element={<MpcFillPage />} />
          <Route path="/backs" element={<BacksPage />} />
          <Route path="/pdf" element={<PdfPage />} />
          <Route path="/export" element={<ExportPage />} />
          <Route path="/tasks" element={<TasksPage />} />
        </Routes>
      </div>
    </ProjectProvider>
  );
}
