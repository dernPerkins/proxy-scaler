// PROTOTYPE — throwaway. Three layouts for the PDF tab's Guides section,
// switchable on /pdf via `?variant=A|B|C` (see PrototypeSwitcher). State is
// a local stub seeded from the project; nothing here is saved or sent to the
// server. Delete this file once a variant is picked and built for real.
import { useState, type CSSProperties, type ReactNode } from "react";
import NumberInput from "../components/NumberInput";

export const GUIDE_VARIANTS = [
  { key: "A", name: "Stacked Per Kind" },
  { key: "B", name: "Matrix Table" },
  { key: "C", name: "Visual Tiles" },
] as const;

type LineStyle = "solid" | "dashed";
type Corners = "square" | "rounded";

interface GuideState {
  width_pt: number;
  length_mm: number;
  card_color: string;
  page_color: string;
  card_style: LineStyle;
  page_style: LineStyle;
  card_corners: Corners;
  hide_card_front: boolean;
  hide_page_front: boolean;
  hide_card_back: boolean;
  hide_page_back: boolean;
}

export interface GuideSeed {
  guide_width_pt?: number | null;
  guide_length_mm?: number | null;
  hide_card_guides_front: boolean;
  hide_page_guides_front: boolean;
  hide_card_guides_back: boolean;
  hide_page_guides_back: boolean;
  back_printing?: boolean;
}

function useStub(seed: GuideSeed) {
  return useState<GuideState>(() => ({
    width_pt: seed.guide_width_pt ?? 0.75,
    length_mm: seed.guide_length_mm ?? 2.75,
    card_color: "#00AA50",
    page_color: "#000000",
    card_style: "solid",
    page_style: "solid",
    card_corners: "square",
    hide_card_front: seed.hide_card_guides_front,
    hide_page_front: seed.hide_page_guides_front,
    hide_card_back: seed.hide_card_guides_back,
    hide_page_back: seed.hide_page_guides_back,
  }));
}

// --- Shared bits -------------------------------------------------------------

const HEX = /^#[0-9a-fA-F]{6}$/;

function ColorField({ value, onChange, compact }: { value: string; onChange: (v: string) => void; compact?: boolean }) {
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <input
        type="color"
        value={value}
        onChange={(e) => onChange(e.target.value.toUpperCase())}
        style={{
          width: compact ? 30 : 44,
          height: 30,
          padding: 2,
          border: "1px solid var(--border-strong)",
          borderRadius: "var(--radius)",
          background: "var(--surface)",
          cursor: "pointer",
          flex: compact ? "none" : 1,
        }}
      />
      <input
        type="text"
        value={draft ?? value}
        spellCheck={false}
        onChange={(e) => {
          const v = e.target.value.trim();
          setDraft(v);
          const withHash = v.startsWith("#") ? v : `#${v}`;
          if (HEX.test(withHash)) onChange(withHash.toUpperCase());
        }}
        onBlur={() => setDraft(null)}
        style={{ width: 86, fontFamily: "ui-monospace, monospace", textTransform: "uppercase" }}
      />
    </div>
  );
}

function Seg<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={value === o.value ? "active" : ""}
          onClick={() => onChange(o.value)}
          style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Switch({ on, onChange, disabled }: { on: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={() => onChange(!on)}
      style={{
        width: 34,
        height: 20,
        padding: 0,
        borderRadius: 999,
        border: "none",
        position: "relative",
        background: on ? "var(--accent)" : "var(--surface-3)",
        opacity: disabled ? 0.45 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
        transition: "background 120ms",
        flex: "none",
      }}
    >
      <span
        style={{
          position: "absolute",
          top: 2,
          left: on ? 16 : 2,
          width: 16,
          height: 16,
          borderRadius: 999,
          background: "#fff",
          boxShadow: "0 1px 2px rgba(0,0,0,0.3)",
          transition: "left 120ms",
        }}
      />
    </button>
  );
}

// A card with its corner marks, drawn the way the PDF would.
function CardGuideIcon({
  corners,
  style,
  color,
  size = 34,
}: {
  corners: Corners;
  style: LineStyle;
  color: string;
  size?: number;
}) {
  const w = 24;
  const h = 32;
  const pad = 6;
  const r = 4;
  const len = 4;
  const dash = style === "dashed" ? "2 1.6" : undefined;
  const pts: [number, number, number, number][] = [
    [pad, pad, 1, 1],
    [pad + w, pad, -1, 1],
    [pad, pad + h, 1, -1],
    [pad + w, pad + h, -1, -1],
  ];
  return (
    <svg width={size * ((w + pad * 2) / (h + pad * 2))} height={size} viewBox={`0 0 ${w + pad * 2} ${h + pad * 2}`}>
      <rect x={pad} y={pad} width={w} height={h} rx={r} fill="var(--text-faint)" opacity={0.45} />
      {pts.map(([x, y, dx, dy], i) =>
        corners === "square" ? (
          <path
            key={i}
            d={`M${x - len} ${y}H${x + len}M${x} ${y - len}V${y + len}`}
            stroke={color}
            strokeWidth={2}
            strokeDasharray={dash}
            fill="none"
          />
        ) : (
          <path
            key={i}
            d={`M${x + dx * (r + len)} ${y}H${x + dx * r}A${r} ${r} 0 0 ${dx * dy > 0 ? 0 : 1} ${x} ${y + dy * r}V${y + dy * (r + len)}`}
            stroke={color}
            strokeWidth={2}
            strokeDasharray={dash}
            fill="none"
          />
        ),
      )}
    </svg>
  );
}

// A page with its edge-to-grid lines.
function PageGuideIcon({ style, color, size = 34 }: { style: LineStyle; color: string; size?: number }) {
  const dash = style === "dashed" ? "2 1.6" : undefined;
  const W = 30;
  const H = 38;
  const gx0 = 9;
  const gx1 = 21;
  const gy0 = 11;
  const gy1 = 27;
  return (
    <svg width={size * (W / H)} height={size} viewBox={`0 0 ${W} ${H}`}>
      <rect x={0.5} y={0.5} width={W - 1} height={H - 1} rx={2} fill="var(--surface)" stroke="var(--border-strong)" />
      <rect x={gx0} y={gy0} width={gx1 - gx0} height={gy1 - gy0} fill="var(--text-faint)" opacity={0.45} />
      <path
        d={`M${gx0} 1V${gy0}M${gx1} 1V${gy0}M${gx0} ${gy1}V${H - 1}M${gx1} ${gy1}V${H - 1}M1 ${gy0}H${gx0}M1 ${gy1}H${gx0}M${gx1} ${gy0}H${W - 1}M${gx1} ${gy1}H${W - 1}`}
        stroke={color}
        strokeWidth={1.8}
        strokeDasharray={dash}
        fill="none"
      />
    </svg>
  );
}

function StateReadout({ s }: { s: GuideState }) {
  return (
    <pre
      style={{
        marginTop: 14,
        padding: 8,
        fontSize: 10.5,
        lineHeight: 1.35,
        background: "var(--surface-2)",
        border: "1px dashed #ff2d87",
        borderRadius: "var(--radius)",
        color: "var(--text-muted)",
        whiteSpace: "pre-wrap",
      }}
    >
      {"PROTOTYPE STATE (not saved)\n" + JSON.stringify(s, null, 1).replace(/[{}"]/g, "").trim()}
    </pre>
  );
}

function GuidesHeading() {
  return (
    <h3 style={{ marginBottom: 14 }}>
      Guides{" "}
      <span
        className="hint"
        title="Front pages are the side you cut against, so guides there are the ones you use. Back pages default to none — that ink lands on the visible side of the card."
      >
        (?)
      </span>
    </h3>
  );
}

const LINE_OPTIONS: { value: LineStyle; label: string }[] = [
  { value: "solid", label: "Solid" },
  { value: "dashed", label: "Dashed" },
];
const CORNER_OPTIONS: { value: Corners; label: string }[] = [
  { value: "square", label: "Square" },
  { value: "rounded", label: "Rounded" },
];

const backsOffTitle = "Turn on Back Printing to print back pages";

// --- Variant A: one bordered block per guide kind ---------------------------

function KindBlock({
  title,
  icon,
  children,
}: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <div
      style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
        padding: 12,
        background: "var(--surface)",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <strong style={{ fontSize: 13 }}>{title}</strong>
        {icon}
      </div>
      {children}
    </div>
  );
}

function ShowOn({
  front,
  back,
  onFront,
  onBack,
  backDisabled,
}: {
  front: boolean;
  back: boolean;
  onFront: (v: boolean) => void;
  onBack: (v: boolean) => void;
  backDisabled: boolean;
}) {
  const chip = (on: boolean, label: string, set: (v: boolean) => void, disabled = false) => (
    <button
      type="button"
      aria-pressed={on}
      disabled={disabled}
      title={disabled ? backsOffTitle : undefined}
      onClick={() => set(!on)}
      style={{
        flex: 1,
        padding: "6px 8px",
        fontSize: 12.5,
        borderRadius: "var(--radius)",
        border: `1px solid ${on ? "var(--accent)" : "var(--border-strong)"}`,
        background: on ? "var(--accent-soft)" : "var(--surface)",
        color: on ? "var(--accent)" : "var(--text-muted)",
        fontWeight: on ? 600 : 500,
        opacity: disabled ? 0.45 : 1,
      }}
    >
      {on ? "✓ " : ""}
      {label}
    </button>
  );
  return (
    <div className="field">
      <span>Show On</span>
      <div style={{ display: "flex", gap: 6 }}>
        {chip(front, "Fronts", onFront)}
        {chip(back, "Backs", onBack, backDisabled)}
      </div>
    </div>
  );
}

export function VariantA({ seed }: { seed: GuideSeed }) {
  const [s, set] = useStub(seed);
  const up = <K extends keyof GuideState>(k: K, v: GuideState[K]) => set((p) => ({ ...p, [k]: v }));
  return (
    <>
      <GuidesHeading />
      <div className="field-group" data-tour="pdf-guides">
        <div className="field">
          <span>Guide Width (pt)</span>
          <NumberInput step={0.05} value={s.width_pt} onChange={(v) => up("width_pt", v)} />
        </div>

        <KindBlock
          title="Card Guides"
          icon={<CardGuideIcon corners={s.card_corners} style={s.card_style} color={s.card_color} size={30} />}
        >
          <div className="field">
            <span>Color</span>
            <ColorField value={s.card_color} onChange={(v) => up("card_color", v)} />
          </div>
          <div className="field">
            <span>Line</span>
            <Seg value={s.card_style} options={LINE_OPTIONS} onChange={(v) => up("card_style", v)} />
          </div>
          <div className="field">
            <span>Corners</span>
            <Seg value={s.card_corners} options={CORNER_OPTIONS} onChange={(v) => up("card_corners", v)} />
          </div>
          <div className="field">
            <span>Length (mm)</span>
            <NumberInput step={0.1} value={s.length_mm} onChange={(v) => up("length_mm", v)} />
          </div>
          <ShowOn
            front={!s.hide_card_front}
            back={!s.hide_card_back}
            onFront={(v) => up("hide_card_front", !v)}
            onBack={(v) => up("hide_card_back", !v)}
            backDisabled={!seed.back_printing}
          />
        </KindBlock>

        <KindBlock title="Page Guides" icon={<PageGuideIcon style={s.page_style} color={s.page_color} size={30} />}>
          <div className="field">
            <span>Color</span>
            <ColorField value={s.page_color} onChange={(v) => up("page_color", v)} />
          </div>
          <div className="field">
            <span>Line</span>
            <Seg value={s.page_style} options={LINE_OPTIONS} onChange={(v) => up("page_style", v)} />
          </div>
          <ShowOn
            front={!s.hide_page_front}
            back={!s.hide_page_back}
            onFront={(v) => up("hide_page_front", !v)}
            onBack={(v) => up("hide_page_back", !v)}
            backDisabled={!seed.back_printing}
          />
        </KindBlock>
      </div>
      <StateReadout s={s} />
    </>
  );
}

// --- Variant B: one matrix, kinds as columns ---------------------------------

// Icon-only two-way toggle so a matrix cell (~90px) can hold it; the words
// live in the tooltip.
function IconSeg<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: string; icon: ReactNode }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.label}
          aria-label={o.label}
          className={value === o.value ? "active" : ""}
          onClick={() => onChange(o.value)}
          style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "5px 4px" }}
        >
          {o.icon}
        </button>
      ))}
    </div>
  );
}

const lineIcon = (dashed: boolean) => (
  <svg width="26" height="14" viewBox="0 0 26 14">
    <path d="M2 7H24" stroke="currentColor" strokeWidth="2" strokeDasharray={dashed ? "3 2.5" : undefined} />
  </svg>
);
const cornerIcon = (rounded: boolean) => (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="2">
    {rounded ? <path d="M16 4H10A6 6 0 0 0 4 10V16" /> : <path d="M1 7H13M7 1V13" />}
  </svg>
);

function SwatchHex({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        border: "1px solid var(--border-strong)",
        borderRadius: "var(--radius)",
        padding: "3px 6px 3px 3px",
        background: "var(--surface)",
        minWidth: 0,
        width: "100%",
      }}
    >
      <input
        type="color"
        value={value}
        onChange={(e) => onChange(e.target.value.toUpperCase())}
        style={{ width: 22, height: 22, padding: 0, border: "none", background: "none", cursor: "pointer", flex: "none" }}
      />
      <input
        type="text"
        value={draft ?? value}
        spellCheck={false}
        onChange={(e) => {
          const v = e.target.value.trim();
          setDraft(v);
          const withHash = v.startsWith("#") ? v : `#${v}`;
          if (HEX.test(withHash)) onChange(withHash.toUpperCase());
        }}
        onBlur={() => setDraft(null)}
        style={{
          minWidth: 0,
          width: "100%",
          border: "none",
          padding: 0,
          background: "transparent",
          boxShadow: "none",
          fontFamily: "ui-monospace, monospace",
          fontSize: 12,
          textTransform: "uppercase",
        }}
      />
    </div>
  );
}

export function VariantB({ seed }: { seed: GuideSeed }) {
  const [s, set] = useStub(seed);
  const up = <K extends keyof GuideState>(k: K, v: GuideState[K]) => set((p) => ({ ...p, [k]: v }));
  const rowLabel: CSSProperties = { fontSize: 12, fontWeight: 550, color: "var(--text-muted)" };
  const na = <span style={{ color: "var(--text-faint)", fontSize: 12, textAlign: "center" }}>—</span>;
  const center: CSSProperties = { display: "flex", justifyContent: "center", minWidth: 0 };
  const backs = !seed.back_printing;
  const lineOpts = [
    { value: "solid" as const, label: "Solid", icon: lineIcon(false) },
    { value: "dashed" as const, label: "Dashed", icon: lineIcon(true) },
  ];
  const cornerOpts = [
    { value: "square" as const, label: "Square", icon: cornerIcon(false) },
    { value: "rounded" as const, label: "Rounded", icon: cornerIcon(true) },
  ];
  const rule = <div style={{ gridColumn: "1 / -1", borderTop: "1px solid var(--border)" }} />;
  return (
    <>
      <GuidesHeading />
      <div
        data-tour="pdf-guides"
        style={{
          display: "grid",
          gridTemplateColumns: "auto minmax(0, 1fr) minmax(0, 1fr)",
          gap: "10px 8px",
          alignItems: "center",
        }}
      >
        <span />
        <div style={{ ...center, flexDirection: "column", alignItems: "center", gap: 2, fontSize: 12, fontWeight: 600 }}>
          <CardGuideIcon corners={s.card_corners} style={s.card_style} color={s.card_color} size={30} />
          Card
        </div>
        <div style={{ ...center, flexDirection: "column", alignItems: "center", gap: 2, fontSize: 12, fontWeight: 600 }}>
          <PageGuideIcon style={s.page_style} color={s.page_color} size={30} />
          Page
        </div>

        <span style={rowLabel}>Color</span>
        <SwatchHex value={s.card_color} onChange={(v) => up("card_color", v)} />
        <SwatchHex value={s.page_color} onChange={(v) => up("page_color", v)} />

        <span style={rowLabel}>Line</span>
        <IconSeg value={s.card_style} options={lineOpts} onChange={(v) => up("card_style", v)} />
        <IconSeg value={s.page_style} options={lineOpts} onChange={(v) => up("page_style", v)} />

        <span style={rowLabel}>Corners</span>
        <IconSeg value={s.card_corners} options={cornerOpts} onChange={(v) => up("card_corners", v)} />
        {na}

        <span style={rowLabel}>Length</span>
        <NumberInput step={0.1} value={s.length_mm} onChange={(v) => up("length_mm", v)} />
        {na}

        {rule}

        <span style={rowLabel}>Fronts</span>
        <div style={center}>
          <Switch on={!s.hide_card_front} onChange={(v) => up("hide_card_front", !v)} />
        </div>
        <div style={center}>
          <Switch on={!s.hide_page_front} onChange={(v) => up("hide_page_front", !v)} />
        </div>

        <span style={{ ...rowLabel, opacity: backs ? 0.5 : 1 }} title={backs ? backsOffTitle : undefined}>
          Backs
        </span>
        <div style={center}>
          <Switch on={!s.hide_card_back} disabled={backs} onChange={(v) => up("hide_card_back", !v)} />
        </div>
        <div style={center}>
          <Switch on={!s.hide_page_back} disabled={backs} onChange={(v) => up("hide_page_back", !v)} />
        </div>

        {rule}

        <span style={rowLabel}>Width</span>
        <div style={{ gridColumn: "2 / -1" }}>
          <NumberInput step={0.05} value={s.width_pt} onChange={(v) => up("width_pt", v)} />
        </div>
      </div>
      <p className="hint" style={{ marginTop: 8, fontSize: 11.5 }}>
        Length in mm, width in pt. Fronts / Backs: which pages carry the guides.
      </p>
      <StateReadout s={s} />
    </>
  );
}

// --- Variant C: pick by picture ----------------------------------------------

function Tile({ active, onClick, children, title }: { active: boolean; onClick: () => void; children: ReactNode; title: string }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      aria-pressed={active}
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 6,
        height: 52,
        borderRadius: "var(--radius)",
        border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
        background: active ? "var(--accent-soft)" : "var(--surface-2)",
        boxShadow: active ? "var(--ring)" : "none",
      }}
    >
      {children}
    </button>
  );
}

function EyeChip({
  on,
  onChange,
  disabled,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      disabled={disabled}
      title={disabled ? backsOffTitle : undefined}
      onClick={() => onChange(!on)}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 6,
        padding: "6px 8px",
        fontSize: 12.5,
        borderRadius: "var(--radius)",
        border: `1px solid ${on ? "var(--ok)" : "var(--border-strong)"}`,
        background: on ? "var(--ok-soft)" : "var(--surface)",
        color: on ? "var(--ok)" : "var(--text-faint)",
        fontWeight: 550,
        opacity: disabled ? 0.45 : 1,
      }}
    >
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
        <circle cx="12" cy="12" r="3" />
        {!on && <path d="M3 3l18 18" />}
      </svg>
      {on ? "Shown" : "Hidden"}
    </button>
  );
}

export function VariantC({ seed }: { seed: GuideSeed }) {
  const [s, set] = useStub(seed);
  const up = <K extends keyof GuideState>(k: K, v: GuideState[K]) => set((p) => ({ ...p, [k]: v }));
  const sub: CSSProperties = { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 };
  const subTitle: CSSProperties = { fontSize: 12, fontWeight: 550, color: "var(--text-muted)" };
  const combos: [Corners, LineStyle][] = [
    ["square", "solid"],
    ["square", "dashed"],
    ["rounded", "solid"],
    ["rounded", "dashed"],
  ];
  const backs = !seed.back_printing;
  return (
    <>
      <GuidesHeading />
      <div className="field-group" data-tour="pdf-guides">
        <div style={sub}>
          <span style={subTitle}>Card Guides</span>
          <ColorField compact value={s.card_color} onChange={(v) => up("card_color", v)} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 6 }}>
          {combos.map(([c, l]) => (
            <Tile
              key={c + l}
              title={`${c === "square" ? "Square" : "Rounded"}, ${l === "solid" ? "Solid" : "Dashed"}`}
              active={s.card_corners === c && s.card_style === l}
              onClick={() => set((p) => ({ ...p, card_corners: c, card_style: l }))}
            >
              <CardGuideIcon corners={c} style={l} color={s.card_color} size={38} />
            </Tile>
          ))}
        </div>

        <div style={{ ...sub, marginTop: 4 }}>
          <span style={subTitle}>Page Guides</span>
          <ColorField compact value={s.page_color} onChange={(v) => up("page_color", v)} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 6 }}>
          {LINE_OPTIONS.map((o) => (
            <Tile key={o.value} title={o.label} active={s.page_style === o.value} onClick={() => up("page_style", o.value)}>
              <PageGuideIcon style={o.value} color={s.page_color} size={38} />
            </Tile>
          ))}
        </div>

        <div style={{ display: "flex", gap: 10, marginTop: 4 }}>
          <div className="field" style={{ flex: 1 }}>
            <span>Width (pt)</span>
            <NumberInput step={0.05} value={s.width_pt} onChange={(v) => up("width_pt", v)} />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <span>Length (mm)</span>
            <NumberInput step={0.1} value={s.length_mm} onChange={(v) => up("length_mm", v)} />
          </div>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "auto 1fr 1fr",
            gap: 6,
            alignItems: "center",
            marginTop: 4,
          }}
        >
          <span />
          <span style={{ ...subTitle, textAlign: "center" }}>Fronts</span>
          <span style={{ ...subTitle, textAlign: "center", opacity: backs ? 0.5 : 1 }}>Backs</span>
          <span style={subTitle}>Card</span>
          <EyeChip on={!s.hide_card_front} onChange={(v) => up("hide_card_front", !v)} />
          <EyeChip on={!s.hide_card_back} disabled={backs} onChange={(v) => up("hide_card_back", !v)} />
          <span style={subTitle}>Page</span>
          <EyeChip on={!s.hide_page_front} onChange={(v) => up("hide_page_front", !v)} />
          <EyeChip on={!s.hide_page_back} disabled={backs} onChange={(v) => up("hide_page_back", !v)} />
        </div>
      </div>
      <StateReadout s={s} />
    </>
  );
}
