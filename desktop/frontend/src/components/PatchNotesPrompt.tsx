import { useEffect, useState, useSyncExternalStore } from "react";
import ModalOverlay from "./ModalOverlay";
import { isTauri } from "../tauri";
import { PATCH_NOTES } from "../patchNotes";
import {
  getAppVersion,
  getBootUpdateCheckSettled,
  getPatchNotesRequestSeq,
  getPatchNotesSeenVersion,
  getUpdatePromptOpen,
  setPatchNotesPromptOpen,
  setPatchNotesSeenVersion,
  setPatchNotesSettled,
  subscribeUpdateStore,
} from "../update";

// The Patch Notes dialog. Mounted once in main.tsx, ABOVE ConnectGate
// like UpdatePrompt and for the same reason: it must be able to open on
// launch, while the Local/Remote picker is still the screen — and it
// talks only to Tauri commands, so it needs no connection, router, or
// query client. Auto-opens exactly once per release: any close writes
// the current version as "seen" (no checkbox — closing IS the answer),
// and the next release supersedes that by simply not matching. The
// version number at the end of the tab bar (App.tsx) reopens it anytime,
// via the same cross-tree store signal as the update button.
//
// Second link in the boot-dialog chain (update -> patch notes ->
// resume-tasks -> card-db): waits for the update check to settle and its
// modal to close, and publishes its own settled/open pair for the links
// behind it. Notes that go stale the moment an update installs shouldn't
// outrank the offer to install it — hence update first.
export default function PatchNotesPrompt() {
  const [open, setOpen] = useState(false);
  // Which release the dialog is "about" this launch — used both for the
  // seen-version write on close and the "current" tag in the list.
  const [currentVersion, setCurrentVersion] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);

  const bootUpdateSettled = useSyncExternalStore(
    subscribeUpdateStore,
    getBootUpdateCheckSettled,
  );
  const updatePromptOpen = useSyncExternalStore(subscribeUpdateStore, getUpdatePromptOpen);
  const updateLinkClear = bootUpdateSettled && !updatePromptOpen;

  useEffect(() => {
    if (!isTauri()) {
      // A plain browser dev tab has no version to ask for and nothing to
      // persist — but the chain flag must still settle, or every dialog
      // behind this one waits forever. Dev preview: `?patchnotes` in the
      // URL force-opens the dialog anyway (against the newest entry).
      setPatchNotesSettled();
      if (import.meta.env.DEV && new URLSearchParams(location.search).has("patchnotes")) {
        setCurrentVersion(PATCH_NOTES[0]?.version ?? null);
        setOpen(true);
      }
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const version = await getAppVersion();
        if (cancelled) return;
        setCurrentVersion(version);
        // Only auto-open when this exact release both has notes and
        // hasn't been seen — a build with no entry (dev builds between
        // releases) stays quiet rather than showing stale notes.
        const seen = await getPatchNotesSeenVersion().catch(() => null);
        if (cancelled || seen === version) return;
        if (!PATCH_NOTES.some((e) => e.version === version)) return;
        setOpen(true);
      } catch {
        // No version, no dialog — nothing useful to show without one.
      } finally {
        setPatchNotesSettled();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // The links behind this one sequence themselves on these — published
  // continuously, exactly like UpdatePrompt does for its pair. "Open"
  // deliberately includes waiting-for-the-update-link time only via
  // `open` itself being rendered later; the flag tracks intent to show.
  useEffect(() => {
    setPatchNotesPromptOpen(open);
  }, [open]);

  // The tab-bar version number's re-open signal. Functional setState is
  // unnecessary here (there's only open/closed), but the seq guard
  // matches requestUpdatePrompt's counter contract: every click is a
  // fresh signal. Reopening always lands on the newest entry.
  const requestSeq = useSyncExternalStore(subscribeUpdateStore, getPatchNotesRequestSeq);
  useEffect(() => {
    if (requestSeq === 0) return;
    setSelected(0);
    setOpen(true);
  }, [requestSeq]);

  function dismiss() {
    setOpen(false);
    // Closing is the answer — best-effort write; a failed one just means
    // the notes return next launch, the harmless direction to fail in.
    if (currentVersion != null) {
      setPatchNotesSeenVersion(currentVersion).catch(() => {});
    }
  }

  // Esc closes, same as clicking anywhere that isn't the dialog.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") dismiss();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, currentVersion]);

  // Waiting for the update link keeps the boot auto-open queued, not
  // dropped: `open` stays true and this renders the moment the update
  // modal closes.
  if (!open || !updateLinkClear) return null;
  const entry = PATCH_NOTES[selected];
  if (!entry) return null;

  return (
    <ModalOverlay onClick={dismiss}>
      <div className="modal patch-notes-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Patch Notes</span>
        </div>
        <div className="patch-notes-split">
          {/* Master list: every release, newest first, labelled by bare
              version number. Selection is per-opening — reopening lands
              back on the newest. */}
          <ul className="patch-notes-list">
            {PATCH_NOTES.map((e, i) => (
              <li key={e.version}>
                <button
                  className={i === selected ? "selected" : undefined}
                  onClick={() => setSelected(i)}
                >
                  v{e.version}
                  {e.version === currentVersion && (
                    <span className="patch-notes-current">current</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
          <div className="modal-body patch-notes-detail">
            <p className="patch-notes-heading">
              <strong>v{entry.version}</strong>
              <span className="patch-notes-date">{entry.date}</span>
            </p>
            <ul>
              {entry.notes.map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          </div>
        </div>
        <div className="modal-actions">
          <button autoFocus onClick={dismiss}>
            Close
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
