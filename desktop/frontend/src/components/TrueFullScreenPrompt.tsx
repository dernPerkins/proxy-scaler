import { useEffect, useState } from "react";
import ModalOverlay from "./ModalOverlay";
import { confirmTrueFullScreen, listenTrueFullScreenConfirm } from "../tauri";

// The one-time warning before View → Toggle True Full Screen (macOS). The
// mode hides the menu bar and Dock outright, not just until the pointer
// reaches the edge, so the first time it's chosen Rust asks for this
// instead of entering (fullscreen_menu.rs), and enters on the confirm,
// which also stores that it was seen. Cancelling stores nothing, so the
// warning comes back next time. Renders nothing until asked.
export default function TrueFullScreenPrompt() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void listenTrueFullScreenConfirm(() => setOpen(true)).then((fn) => {
      if (cancelled) fn();
      else unlisten = fn;
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  if (!open) return null;

  const cancel = () => setOpen(false);
  const enter = () => {
    setOpen(false);
    void confirmTrueFullScreen();
  };

  return (
    <ModalOverlay onClick={cancel}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Enter True Full Screen?</span>
        </div>

        <p style={{ marginBottom: 8 }}>
          True Full Screen runs the app right up to the top of the screen, around the notch.
          To make room, the menu bar and Dock are hidden completely while it&apos;s on, and
          won&apos;t appear when you move the pointer to the edge of the screen.
        </p>
        <p style={{ marginBottom: 14 }}>
          Press <kbd>⌘⇧F</kbd> to leave it, or <kbd>⌘Tab</kbd> to switch to another app.
        </p>

        <div className="modal-actions">
          <button className="btn-sm" onClick={cancel}>
            Cancel
          </button>
          <button className="btn-primary" autoFocus onClick={enter}>
            Enter True Full Screen
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
