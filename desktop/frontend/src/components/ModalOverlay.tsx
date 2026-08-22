import type { ReactNode } from "react";
import { createPortal } from "react-dom";

// The one way to render a .modal-overlay backdrop: portaled to
// document.body so the overlay's z-index competes in the ROOT stacking
// context, not wherever the dialog happens to be mounted. This is a real
// bug class, not a nicety — the Decklist sidebar is `position: sticky`,
// which always creates a stacking context, so a dialog mounted inside it
// (server switcher, card-DB modals, the danger-zone confirm) had its
// "fixed, z-index 1000" backdrop trapped below the card list's printing
// picker (z-index 20, root context): the picker drew and clicked straight
// through the open modal.
export default function ModalOverlay({
  onClick,
  className,
  children,
}: {
  /** Backdrop click (dialogs pass their cancel/close action; the inner
   *  panel stops propagation). Omit while busy to make the dialog
   *  undismissable, same contract as before the portal. */
  onClick?: () => void;
  /** Extra class on the backdrop, for the one case that needs to opt OUT
   *  of the default layering: portals stack in MOUNT order at equal
   *  z-index, and UpdatePrompt is mounted above ConnectGate (main.tsx) —
   *  so anything mounted later inside App would paint over an open update
   *  dialog and swallow its clicks. See .modal-overlay-boot. */
  className?: string;
  children: ReactNode;
}) {
  return createPortal(
    <div className={className ? `modal-overlay ${className}` : "modal-overlay"} onClick={onClick}>
      {children}
    </div>,
    document.body,
  );
}
