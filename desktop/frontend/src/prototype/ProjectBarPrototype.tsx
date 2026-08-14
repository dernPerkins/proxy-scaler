// THROWAWAY PROTOTYPE — not production code.
//
// Answers "Prototype: the ProjectBar when a Project is optional"
// (.scratch/optional-projects/issues/05-projectbar-prototype.md).
//
// Three structurally different ProjectBars, switchable via ?variant=A|B|C,
// mounted in place of the real ProjectBar so they butt up against the real
// tabs, page content and styling. All state is stubbed — no invoke(), no
// react-query, no persistence. Nothing here is wired to a real mutation.
//
// Run: cd desktop/frontend && npm run dev, then open /decklist?variant=A
// (not /?variant=A — the "/" route is <Navigate to="/decklist" replace />,
// and a string `to` drops the query string before this gate sees it.)
//
// Delete this directory (and the gate in App.tsx) once a variant wins.

import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

// --- stubbed app states ----------------------------------------------------

type StateKey = "first-launch" | "unnamed-cards" | "unnamed-generated" | "named";

interface Stub {
  label: string;
  /** null = no row exists yet (lazy creation, see ticket 01) */
  projectId: number | null;
  name: string;
  cards: number;
  generated: number;
  savedProjects: { id: number; name: string }[];
}

const STUBS: Record<StateKey, Stub> = {
  "first-launch": {
    label: "First launch — no row at all",
    projectId: null,
    name: "",
    cards: 0,
    generated: 0,
    savedProjects: [],
  },
  "unnamed-cards": {
    label: "Unnamed + cards imported",
    projectId: 3,
    name: "",
    cards: 12,
    generated: 0,
    savedProjects: [],
  },
  "unnamed-generated": {
    label: "Unnamed + images generated",
    projectId: 3,
    name: "",
    cards: 12,
    generated: 8,
    savedProjects: [
      { id: 1, name: "Krenko Goblins" },
      { id: 2, name: "Atraxa Superfriends" },
    ],
  },
  named: {
    label: "Named + saved",
    projectId: 4,
    name: "Krenko Goblins",
    cards: 12,
    generated: 8,
    savedProjects: [
      { id: 1, name: "Krenko Goblins" },
      { id: 2, name: "Atraxa Superfriends" },
    ],
  },
};

const VARIANTS = ["A", "B", "C"] as const;
type VariantKey = (typeof VARIANTS)[number];

const VARIANT_NAMES: Record<VariantKey, string> = {
  A: "Honest chip — today's bar, told the truth",
  B: "Naming is an action — no standing name field",
  C: "Document title — the title is the state",
};

// --- Variant A: keep the shape, make the chip honest -----------------------
//
// Closest to today. The name field stays put; the chip stops lying (it
// currently reads "Saved · #3" the moment a row exists, per ticket 02) and
// starts carrying the card count. The nudge to name is a hint, not a demand.

function VariantA({ s }: { s: Stub }) {
  const [name, setName] = useState(s.name);
  useEffect(() => setName(s.name), [s]);
  const isNamed = s.name !== "";

  return (
    <div className="project-bar panel">
      <span className={isNamed ? "chip chip-saved" : "chip"}>
        {isNamed
          ? `Saved · #${s.projectId}`
          : s.cards === 0
            ? "Nothing yet"
            : `Unsaved · ${s.cards} cards`}
      </span>

      <input
        className="grow"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Project name"
      />
      <button className="btn-primary" disabled={!name.trim()}>
        Save
      </button>
      <button>New</button>
      <button>Save As…</button>

      {!isNamed && s.cards > 0 && (
        <span className="hint">Name it to keep it after you quit.</span>
      )}

      <span className="spacer" />

      {s.savedProjects.length > 0 && (
        <>
          <div className="divider-v" />
          <select defaultValue="">
            <option value="">Load project…</option>
            {s.savedProjects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} (#{p.id})
              </option>
            ))}
          </select>
          <button disabled>Load</button>
          <button className="btn-danger" disabled>
            Delete
          </button>
        </>
      )}
    </div>
  );
}

// --- Variant B: naming is an action you take, not a field you face ---------
//
// The standing name input is gone — it's the single loudest signal that
// naming is step one. Naming becomes a button that opens an inline field,
// i.e. something you do when you decide to, and "Save As" never has to mean
// two things because promote and copy are separate buttons that appear in
// separate states.

function VariantB({ s }: { s: Stub }) {
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  useEffect(() => {
    setNaming(false);
    setName("");
  }, [s]);
  const isNamed = s.name !== "";

  return (
    <div className="project-bar panel">
      {isNamed ? (
        <>
          <span className="chip chip-saved">Saved</span>
          <strong style={{ fontSize: 15 }}>{s.name}</strong>
        </>
      ) : (
        <span className="chip">
          {s.cards === 0 ? "Nothing yet" : `Working · ${s.cards} cards, not saved`}
        </span>
      )}

      {s.generated > 0 && (
        <span className="hint">{s.generated} images generated</span>
      )}

      <span className="spacer" />

      {naming ? (
        <>
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name this project"
            style={{ minWidth: 200 }}
          />
          <button className="btn-primary" disabled={!name.trim()}>
            Save
          </button>
          <button onClick={() => setNaming(false)}>Cancel</button>
        </>
      ) : (
        <>
          {s.cards > 0 && (
            <button className="btn-primary" onClick={() => setNaming(true)}>
              {isNamed ? "Save a copy…" : "Save as project…"}
            </button>
          )}
          <button>New</button>
          {s.savedProjects.length > 0 && (
            <>
              <div className="divider-v" />
              <select defaultValue="">
                <option value="">Open…</option>
                {s.savedProjects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </>
          )}
        </>
      )}
    </div>
  );
}

// --- Variant C: the title is the state ------------------------------------
//
// No chip at all. A document-style title carries everything: muted
// "Untitled" means unnamed, solid text means named. Click to edit, Enter to
// promote. Nothing on screen asks to be filled in before you start, and
// there's no Save button while unnamed because the cards are already
// persisted — which is literally true after ticket 01.

function VariantC({ s }: { s: Stub }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(s.name);
  useEffect(() => {
    setName(s.name);
    setEditing(false);
  }, [s]);
  const isNamed = s.name !== "";

  return (
    <div className="project-bar panel">
      {editing ? (
        <input
          autoFocus
          className="grow"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => setEditing(false)}
          placeholder="Untitled"
          style={{ fontSize: 17, fontWeight: 600 }}
        />
      ) : (
        <button
          onClick={() => setEditing(true)}
          title="Click to name"
          style={{
            background: "none",
            border: "none",
            padding: "2px 4px",
            fontSize: 17,
            fontWeight: 600,
            cursor: "text",
            color: isNamed ? "var(--text)" : "var(--text-faint)",
          }}
        >
          {isNamed ? s.name : "Untitled"}
        </button>
      )}

      {!isNamed && s.cards > 0 && (
        <span className="hint" style={{ marginLeft: -4 }}>
          · not saved
        </span>
      )}

      {s.cards > 0 && (
        <span className="hint">
          {s.cards} cards{s.generated > 0 ? `, ${s.generated} generated` : ""}
        </span>
      )}

      <span className="spacer" />

      <button>New</button>
      <select defaultValue="">
        <option value="">Open…</option>
        {s.savedProjects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </div>
  );
}

// --- the quit modal (ticket 04) -------------------------------------------
//
// Shared across variants — it's the same two-button decision regardless of
// which bar wins. "Not now" is default-focused and says the work survives.

function QuitModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "grid",
        placeItems: "center",
        zIndex: 60,
      }}
      onClick={onClose}
    >
      <div
        className="panel"
        style={{ padding: 20, maxWidth: 420, background: "var(--surface)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ marginTop: 0 }}>Keep this decklist?</h3>
        <p className="hint" style={{ marginTop: 0 }}>
          You have 12 cards and 8 generated images that aren't saved to a
          project. Give it a name to find it in the project list later.
        </p>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Project name"
          style={{ width: "100%", marginBottom: 12 }}
        />
        <label className="hint" style={{ display: "block", marginBottom: 14 }}>
          <input type="checkbox" /> Don't ask again
        </label>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button autoFocus onClick={onClose}>
            Not now
          </button>
          <button className="btn-primary" disabled={!name.trim()} onClick={onClose}>
            Name &amp; save
          </button>
        </div>
        <p className="hint" style={{ marginBottom: 0, marginTop: 12 }}>
          Either way it'll still be here next time you open the app.
        </p>
      </div>
    </div>
  );
}

// --- switcher + host -------------------------------------------------------

export default function ProjectBarPrototype() {
  const [params, setParams] = useSearchParams();
  const variant = (params.get("variant") ?? "A").toUpperCase() as VariantKey;
  const current = VARIANTS.includes(variant) ? variant : "A";
  const [stateKey, setStateKey] = useState<StateKey>("unnamed-cards");
  const [quitOpen, setQuitOpen] = useState(false);

  function go(delta: number) {
    const i = (VARIANTS.indexOf(current) + delta + VARIANTS.length) % VARIANTS.length;
    const next = new URLSearchParams(params);
    next.set("variant", VARIANTS[i]);
    setParams(next, { replace: true });
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = document.activeElement;
      const typing =
        el instanceof HTMLInputElement ||
        el instanceof HTMLTextAreaElement ||
        (el as HTMLElement | null)?.isContentEditable;
      if (typing) return;
      if (e.key === "ArrowLeft") go(-1);
      if (e.key === "ArrowRight") go(1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const stub = STUBS[stateKey];

  return (
    <>
      {current === "A" && <VariantA s={stub} />}
      {current === "B" && <VariantB s={stub} />}
      {current === "C" && <VariantC s={stub} />}

      {quitOpen && <QuitModal onClose={() => setQuitOpen(false)} />}

      <div
        style={{
          position: "fixed",
          bottom: 16,
          left: "50%",
          transform: "translateX(-50%)",
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "8px 12px",
          borderRadius: 999,
          background: "#111",
          color: "#fff",
          boxShadow: "0 6px 24px rgba(0,0,0,0.35)",
          zIndex: 50,
          fontSize: 13,
          flexWrap: "wrap",
          maxWidth: "94vw",
        }}
      >
        <button onClick={() => go(-1)} style={pill}>
          ←
        </button>
        <strong style={{ whiteSpace: "nowrap" }}>
          {current} — {VARIANT_NAMES[current]}
        </strong>
        <button onClick={() => go(1)} style={pill}>
          →
        </button>
        <span style={{ opacity: 0.4 }}>|</span>
        <select
          value={stateKey}
          onChange={(e) => setStateKey(e.target.value as StateKey)}
          style={{ fontSize: 12 }}
        >
          {Object.entries(STUBS).map(([k, v]) => (
            <option key={k} value={k}>
              {v.label}
            </option>
          ))}
        </select>
        <button onClick={() => setQuitOpen(true)} style={pill}>
          Quit modal
        </button>
      </div>
    </>
  );
}

const pill: React.CSSProperties = {
  background: "#333",
  color: "#fff",
  border: "1px solid #555",
  borderRadius: 999,
  padding: "2px 10px",
  cursor: "pointer",
};
