import { useEffect, type ReactNode } from "react";

import ModalOverlay from "./ModalOverlay";

export interface ConfirmDialogProps {
  title: string;
  /** What is about to happen, in the user's terms. */
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

// The in-app replacement for `window.confirm()`, and the reason it exists:
// a native confirm never appears inside Tauri's WKWebView, so on macOS the
// discard it was guarding went ahead unasked
// (.scratch/optional-projects/issues/16-the-discard-confirm-never-shows-on-macos.md).
// SwitchServerDialog's comment had already written that rule down; this is
// the rule factored out, so the next yes/no question has somewhere to go
// instead of reaching for the native one again.
//
// Follows CompareDialog's pattern — the overlay cancels on click, the
// inner panel stops propagation — and adds Esc, which QuitPrompt
// deliberately does not have. The difference is what a stray dismissal
// costs: there it would quit the app, here it is Cancel, the answer that
// changes nothing. Cancel is focused for the same reason.
//
// The confirm button is btn-danger unconditionally rather than behind a
// prop: both questions this asks today destroy something, which is why
// they are asked at all. A confirm that doesn't is when to add the prop.
export default function ConfirmDialog({
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  // ModalOverlay portals to document.body — this dialog gets mounted from
  // deep inside panels (e.g. the sidebar's CardDbPanel), and an ancestor
  // there can open its own stacking context; see ModalOverlay.tsx.
  return (
    <ModalOverlay onClick={onCancel}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">{title}</span>
        </div>

        <p style={{ marginBottom: 16 }}>{children}</p>

        <div className="modal-actions">
          <button autoFocus onClick={onCancel}>
            Cancel
          </button>
          <button className="btn-danger" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
