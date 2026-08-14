import { useEffect, useRef, useState } from "react";
import { useProject } from "../context/ProjectContext";

const SAVE_TOAST_MS = 4000;

// Owns two independent things that happen to share one component: the
// global Ctrl+S/Cmd+S listener, and the toast that reports the outcome of
// any save — that one fires for a click on ProjectBar's Save button too,
// since both paths go through the same saveMutation (see
// ProjectContext.tsx's saveResult).
export default function SaveShortcutToast() {
  const project = useProject();
  const [visible, setVisible] = useState(false);

  // The context's `value` object is a fresh literal every provider
  // render, so a keydown listener registered once (empty deps) would
  // otherwise close over a stale `project`. Read it through a ref kept
  // current every render instead of re-subscribing on every keystroke
  // elsewhere in the app.
  const projectRef = useRef(project);
  projectRef.current = project;

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== "s") return;
      e.preventDefault(); // Ctrl/Cmd+S is the browser's native "save page"
      const p = projectRef.current;
      if (!p.saving) p.save();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const resultId = project.saveResult?.id;
  useEffect(() => {
    if (resultId === undefined) return;
    setVisible(true);
    if (project.saveResult?.ok) {
      const timer = setTimeout(() => setVisible(false), SAVE_TOAST_MS);
      return () => clearTimeout(timer);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resultId]);

  if (!visible || !project.saveResult) return null;

  if (project.saveResult.ok) {
    return (
      <div className="toast toast-ok toast-secondary">
        <span className="dot" />
        <span className="toast-body">Save successful.</span>
      </div>
    );
  }

  return (
    <div className="toast toast-err toast-secondary">
      <span className="dot" />
      <div className="toast-body">
        <strong>Save failed.</strong>
        <div style={{ marginTop: 2 }}>{project.saveResult.message}</div>
        <button className="btn-sm" style={{ marginTop: 8 }} onClick={() => setVisible(false)}>
          Dismiss
        </button>
      </div>
    </div>
  );
}
