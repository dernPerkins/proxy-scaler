import { useEffect, useRef, useState } from "react";
import { projectApi } from "../api/project";
import { useProject } from "../context/ProjectContext";
import ModalOverlay from "./ModalOverlay";
import {
  invokeAnswerQuitPrompt,
  invokeQuitPromptListening,
  listenCloseRequested,
} from "../tauri";

// The offer to name an Unnamed Project on the way out
// (.scratch/optional-projects/spec.md §6). An offer, never a toll gate:
// both buttons quit, and the work survives either way — cards and settings
// are written through as they are made (§5.2), and the handler below lands
// every debounced write still in a timer — settings, and a name typed into
// the bar — before it answers, so there is nothing here to rescue. What a
// name buys is finding this project again in the Load list,
// which is why none of the copy below says "unsaved".
//
// Discard is deliberately not offered here; it stays an in-app action on
// New (§5.6). That also keeps every HTTP call off the shutdown path, where
// nothing outlives Rust's std::process::exit.
//
// Two halves talk to each other across the IPC boundary:
//
//   - main.rs::on_window_event prevents the close, arms a channel and
//     waits for this component's answer before hiding the window and
//     stopping the sidecar.
//   - this component decides whether there is anything to ask, says so
//     (`prompting` / `proceed`), and releases the teardown when the user
//     has chosen.
//
// If this component never answers — the webview failed to load, or wedged
// — Rust gives up after a few seconds and quits anyway, which lands on
// exactly the "Not now" outcome.
//
// Gestures that never reach here at all: macOS Cmd+Q and menu-bar Quit
// (they fire no close event), and Ctrl+C in the terminal (a signal, not a
// window event). Both skip the prompt, and both are safe for the same
// reason — an offer that doesn't appear leaves exactly the "Not now"
// outcome behind. Reasoning:
// .scratch/optional-projects/decisions/04-quit-prompt-close-path.md.
export default function QuitPrompt() {
  const project = useProject();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [dontAskAgain, setDontAskAgain] = useState(false);
  // A button has been pressed and the answer is on its way to Rust — so
  // nothing here takes a second press. Cleared again only where the
  // leaving turned out not to happen: a name that collided.
  const [leaving, setLeaving] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);

  // The close handler is registered once, so it reads the current project
  // through a ref rather than closing over the render it was created in.
  const latest = useRef({
    isNamed: project.isNamed,
    cardCount: project.cards.length,
    flushPendingWrites: project.flushPendingWrites,
  });
  useEffect(() => {
    latest.current = {
      isNamed: project.isNamed,
      cardCount: project.cards.length,
      flushPendingWrites: project.flushPendingWrites,
    };
  });

  // Read once at mount and held as the promise, not the value: a quit in
  // the first moments of a launch must wait for the real answer rather
  // than fall back to a default that would show a prompt the user has
  // already switched off. A failed read means "still offer it".
  const suppressed = useRef<Promise<boolean> | null>(null);
  useEffect(() => {
    suppressed.current = projectApi.getQuitPromptSuppressed().catch(() => false);
  }, []);

  // Read and written by the close handler, not by React: a second close
  // request has to be recognised before the next render.
  const asked = useRef(false);

  useEffect(() => {
    let unlisten: (() => void) | null = null;
    let cancelled = false;
    void (async () => {
      const stop = await listenCloseRequested(async (event) => {
        // Not optional: without it Tauri's own wrapper destroys the window
        // the moment this handler returns, mid-teardown. See
        // tauri.ts::listenCloseRequested.
        event.preventDefault();
        // The window stays live and clickable behind the modal, so the X
        // can be pressed again while the offer is up. Rust's own guard
        // stops teardown running twice; this stops the second click's
        // answer being read as the one the modal is still waiting for.
        if (asked.current) return;
        asked.current = true;
        // Before either answer, because both of them end in the process
        // exiting: a settings change — or a name typed into the bar — from
        // the last few hundred milliseconds is still in a debounce timer,
        // and the copy below promises it is already stored.
        await latest.current.flushPendingWrites();
        // Read after the flush, and knowingly a render behind it: a name
        // that just landed has set state that React may not have committed
        // yet, so an Unnamed Project named inside the last 500ms can still
        // be offered this prompt. Harmless in both directions — the name is
        // in the store either way, and "Not now" is one keystroke away —
        // and not worth a round of machinery to chase.
        const { isNamed, cardCount } = latest.current;
        const skip =
          isNamed || cardCount === 0 || (await (suppressed.current ?? Promise.resolve(false)));
        if (skip) {
          await invokeAnswerQuitPrompt("proceed");
          return;
        }
        // Said before the modal renders, so Rust stops counting down the
        // moment the answer is a person's to give.
        await invokeAnswerQuitPrompt("prompting");
        setOpen(true);
      });
      if (cancelled) {
        stop();
        return;
      }
      unlisten = stop;
      // Only now, with the listener actually live: before this, the close
      // path is right not to wait for an answer that cannot come. The
      // reverse order would be worse than the millisecond it leaves
      // uncovered — Rust would sit out its whole timeout waiting on a
      // handler that is not registered yet.
      await invokeQuitPromptListening();
    })();
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  if (!open) return null;

  async function leave() {
    setLeaving(true);
    if (dontAskAgain) {
      // Awaited, not fired loose: the teardown this releases ends in
      // std::process::exit, and a write still in flight would die with it
      // — leaving the prompt to reappear next launch.
      try {
        await projectApi.setQuitPromptSuppressed(true);
      } catch {
        // A setting that didn't stick is not worth refusing the quit the
        // user has already asked for.
      }
    }
    await invokeAnswerQuitPrompt("proceed");
    // No setLeaving(false): the window goes away from here.
  }

  async function nameAndSave() {
    const trimmed = name.trim();
    if (!trimmed) return;
    setLeaving(true);
    setNameError(null);
    try {
      await project.rename(trimmed);
    } catch (err) {
      // A collision, typically. The quit stays parked — Rust is still
      // waiting on this modal — so the user can pick another name or take
      // "Not now" and go.
      setNameError(err instanceof Error ? err.message : String(err));
      setLeaving(false);
      return;
    }
    await leave();
  }

  const cardCount = project.cards.length;

  return (
    // The two buttons are the whole choice: no dismiss on the overlay and
    // no Esc handler, unlike the app's other dialogs. Both would be a
    // keystroke or a stray click that conventionally closes a dialog
    // quietly terminating the app instead. "Not now" is focused, so Enter
    // and Space already reach the way out.
    <ModalOverlay>
      <div className="modal modal-sm">
        <div className="modal-head">
          <span className="modal-title">Name this project before closing?</span>
        </div>

        <p style={{ marginBottom: 14 }}>
          Not now keeps everything: your {cardCount} {cardCount === 1 ? "card" : "cards"}{" "}
          and settings are already stored and will be here next time you open the app. A
          name is what makes this project findable in the project list later.
        </p>

        <label className="field" style={{ marginBottom: 12 }}>
          <span>Project name</span>
          <input
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              // The error described a name the user has now moved past.
              setNameError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !leaving) void nameAndSave();
            }}
            placeholder="Krenko Goblins"
            disabled={leaving}
          />
        </label>

        {nameError && (
          <p className="error-text" style={{ marginBottom: 12 }}>
            {nameError}
          </p>
        )}

        <label className="check" style={{ marginBottom: 14 }}>
          <input
            type="checkbox"
            checked={dontAskAgain}
            onChange={(e) => setDontAskAgain(e.target.checked)}
            disabled={leaving}
          />
          <span>Don&apos;t ask again</span>
        </label>

        <div className="modal-actions">
          {/* Default-focused, and the one that needs no thought: nothing
              is lost by taking it. */}
          <button autoFocus onClick={() => void leave()} disabled={leaving}>
            Not now
          </button>
          {/* The bar dropped its Save button for a debounce, but this is
              the app disappearing rather than a field you are sitting in
              — the confirm has to be explicit. */}
          <button
            className="btn-primary"
            onClick={() => void nameAndSave()}
            disabled={leaving || !name.trim()}
          >
            Name &amp; save
          </button>
        </div>
      </div>
    </ModalOverlay>
  );
}
