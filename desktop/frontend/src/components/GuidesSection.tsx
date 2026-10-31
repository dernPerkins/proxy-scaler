// The PDF tab's Guides section: one matrix with a column per guide kind
// (Card Guides at each card's trim corners, Page Guides from the page edge
// in to the grid) and a row per setting, so the two kinds' settings read
// side by side. Corners and Length are Card Guides only. Radius (and its
// Custom Radius) spans both: it is the card's corner, whichever kind of
// guide follows it, on fronts and backs alike.
//
// The visibility rows show the stored HIDE flags (see
// pdf_layout.GuideVisibility for why they are stored that way round) as
// Shown / Hidden, which reads as a state rather than as a double negative.
import type { ReactNode } from "react";
import type { ProjectSettings } from "../api/project";
import type { CornerRadius, GuideCorners, GuideLine } from "../api/types";
import { GUIDE_STYLES_MIN_SERVER_VERSION } from "../config";
import ColorInput from "./ColorInput";
import NumberInput from "./NumberInput";
import SelectMenu from "./SelectMenu";

type GuideSettings = Pick<
  ProjectSettings,
  | "guide_width_pt"
  | "guide_length_mm"
  | "page_guide_width_pt"
  | "card_guide_color"
  | "page_guide_color"
  | "card_guide_line"
  | "page_guide_line"
  | "card_guide_corners"
  | "corner_radius"
  | "custom_corner_radius_mm"
  | "hide_card_guides_front"
  | "hide_page_guides_front"
  | "hide_card_guides_back"
  | "hide_page_guides_back"
>;

const BACKS_OFF_TITLE = "Turn on Back Printing to print back pages";
const STYLES_UNSUPPORTED_TITLE = `Needs a v${GUIDE_STYLES_MIN_SERVER_VERSION} or newer generation server`;
export const ALPHA_CUT_FILE_NOTE =
  "Sheets With Alpha Cards Get Their Own Cut File — Downloads as a ZIP, One SVG per Sheet";
const RADIUS_TITLE =
  "Match the corner your cutter makes. Modern: every card at 2.5 mm. " +
  "Proper Alpha Cards: Alpha printings at Alpha's rounder 4 mm, the rest modern. " +
  "All Alpha Cards: every card at 4 mm. Custom: every card at the radius you set. " +
  "Applies to rounded Card Guides and the cut file; with Proper Alpha Cards, each " +
  "sheet holding Alpha cards gets its own cut file, in a ZIP.";

// A two-way choice small enough for a matrix cell: icons, with the words
// in the tooltip and accessible name.
function IconToggle<T extends string>({
  value,
  options,
  onChange,
  disabled,
  title,
}: {
  value: T;
  options: readonly [
    { value: T; label: string; icon: ReactNode },
    { value: T; label: string; icon: ReactNode },
  ];
  onChange: (value: T) => void;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <div className="segmented icon-segmented" title={title}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          title={disabled ? undefined : option.label}
          aria-label={option.label}
          aria-pressed={value === option.value}
          className={value === option.value ? "active" : ""}
          disabled={disabled}
          onClick={() => onChange(option.value)}
        >
          {option.icon}
        </button>
      ))}
    </div>
  );
}

function VisibilityChip({
  shown,
  onChange,
  disabled,
  label,
}: {
  shown: boolean;
  onChange: (shown: boolean) => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      className={`visibility-chip${shown ? " is-shown" : ""}`}
      aria-pressed={shown}
      aria-label={label}
      disabled={disabled}
      title={disabled ? BACKS_OFF_TITLE : undefined}
      onClick={() => onChange(!shown)}
    >
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
        <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
        <circle cx="12" cy="12" r="3" />
        {!shown && <path d="M3 3l18 18" />}
      </svg>
      {shown ? "Shown" : "Hidden"}
    </button>
  );
}

const lineIcon = (dashed: boolean) => (
  <svg width="26" height="14" viewBox="0 0 26 14" aria-hidden="true">
    <path d="M2 7H24" stroke="currentColor" strokeWidth="2" strokeDasharray={dashed ? "3 2.5" : undefined} />
  </svg>
);

const cornerIcon = (rounded: boolean) => (
  <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    {rounded ? <path d="M16 4H10A6 6 0 0 0 4 10V16" /> : <path d="M1 7H13M7 1V13" />}
  </svg>
);

const LINE_OPTIONS = [
  { value: "solid", label: "Solid", icon: lineIcon(false) },
  { value: "dashed", label: "Dashed", icon: lineIcon(true) },
] as const satisfies readonly { value: GuideLine; label: string; icon: ReactNode }[];

const CORNER_OPTIONS = [
  { value: "square", label: "Square", icon: cornerIcon(false) },
  { value: "rounded", label: "Rounded", icon: cornerIcon(true) },
] as const satisfies readonly { value: GuideCorners; label: string; icon: ReactNode }[];

// Short in the cell, spelled out in the open list.
const RADIUS_OPTIONS: { value: CornerRadius; label: string }[] = [
  { value: "modern", label: "Modern" },
  { value: "alpha_cards", label: "Proper Alpha Cards" },
  { value: "all_cards", label: "All Alpha Cards" },
  { value: "custom", label: "Custom" },
];

export default function GuidesSection({
  values,
  onChange,
  backPrinting,
  stylesSupported,
}: {
  values: GuideSettings;
  onChange: <K extends keyof GuideSettings>(key: K, value: GuideSettings[K]) => void;
  backPrinting: boolean;
  /** False against a server older than guide styling: only the Card
   *  Guides' width and length (which every server understands) and the
   *  visibility flags stay editable. */
  stylesSupported: boolean;
}) {
  const styleTitle = stylesSupported ? undefined : STYLES_UNSUPPORTED_TITLE;
  const notApplicable = <span className="guides-na" aria-hidden="true">—</span>;

  return (
    <>
      {/* No top margin: this is the column's first heading and has to
          sit level with "Source images" in the other column. */}
      <h3 style={{ marginBottom: 14 }}>
        Guides{" "}
        <span
          className="hint"
          title="Card Guides mark each card's trim corners; Page Guides run from the page edge in to the grid. Fronts are the side you cut against, so guides there are the ones you use. Backs default to Hidden — that ink lands on the visible side of the card."
        >
          (?)
        </span>
      </h3>

      <div className="guides-matrix" data-tour="pdf-guides">
        {/* The corner cell holds the longest label, invisibly, so the
            label column is that wide whether or not the Custom Radius row
            is showing: choosing Custom doesn't shift the columns. */}
        <span className="guides-row-label guides-label-sizer" aria-hidden="true">
          Custom Radius (mm)
        </span>
        <span className="guides-col-head">Card</span>
        <span className="guides-col-head">Page</span>

        <span className="guides-row-label">Color</span>
        <ColorInput
          ariaLabel="Card Guide color"
          value={values.card_guide_color}
          onChange={(v) => onChange("card_guide_color", v)}
          disabled={!stylesSupported}
          title={styleTitle}
        />
        <ColorInput
          ariaLabel="Page Guide color"
          value={values.page_guide_color}
          onChange={(v) => onChange("page_guide_color", v)}
          disabled={!stylesSupported}
          title={styleTitle}
        />

        <span className="guides-row-label">Line</span>
        <IconToggle
          value={values.card_guide_line}
          options={LINE_OPTIONS}
          onChange={(v) => onChange("card_guide_line", v)}
          disabled={!stylesSupported}
          title={styleTitle}
        />
        <IconToggle
          value={values.page_guide_line}
          options={LINE_OPTIONS}
          onChange={(v) => onChange("page_guide_line", v)}
          disabled={!stylesSupported}
          title={styleTitle}
        />

        <span className="guides-row-label">Corners</span>
        <IconToggle
          value={values.card_guide_corners}
          options={CORNER_OPTIONS}
          onChange={(v) => onChange("card_guide_corners", v)}
          disabled={!stylesSupported}
          title={styleTitle}
        />
        {notApplicable}

        <span className="guides-row-label">
          Radius{" "}
          <span className="hint" title={RADIUS_TITLE}>
            (?)
          </span>
        </span>
        <div className="guides-span">
          <SelectMenu
            ariaLabel="Radius"
            value={values.corner_radius}
            options={RADIUS_OPTIONS}
            onChange={(v) => onChange("corner_radius", v as CornerRadius)}
            disabled={!stylesSupported}
            title={styleTitle}
          />
        </div>

        {values.corner_radius === "custom" && (
          <>
            <span className="guides-row-label" title="Every card's corner radius, in millimetres">
              Custom Radius (mm)
            </span>
            <div className="guides-span" title={styleTitle}>
              <NumberInput
                step={0.1}
                min={0}
                value={values.custom_corner_radius_mm}
                onChange={(v) => onChange("custom_corner_radius_mm", v)}
                disabled={!stylesSupported}
              />
            </div>
          </>
        )}

        <span className="guides-row-label" title="Stroke width, in points">
          Width (pt)
        </span>
        <NumberInput
          step={0.05}
          min={0}
          value={values.guide_width_pt}
          onChange={(v) => onChange("guide_width_pt", v)}
        />
        <div title={styleTitle}>
          <NumberInput
            step={0.05}
            min={0}
            value={values.page_guide_width_pt ?? values.guide_width_pt}
            onChange={(v) => onChange("page_guide_width_pt", v)}
            disabled={!stylesSupported}
          />
        </div>

        <span className="guides-row-label" title="How far each Card Guide runs along the card's edges, in millimetres">
          Length (mm)
        </span>
        <NumberInput
          step={0.1}
          min={0}
          value={values.guide_length_mm}
          onChange={(v) => onChange("guide_length_mm", v)}
        />
        {notApplicable}

        <div className="guides-rule" />

        <span className="guides-row-label">Fronts</span>
        <VisibilityChip
          label="Card Guides on front pages"
          shown={!values.hide_card_guides_front}
          onChange={(shown) => onChange("hide_card_guides_front", !shown)}
        />
        <VisibilityChip
          label="Page Guides on front pages"
          shown={!values.hide_page_guides_front}
          onChange={(shown) => onChange("hide_page_guides_front", !shown)}
        />

        {/* Still shown (not hidden) while back printing is off, so the
            settings stay comparable side by side — just inert, and said
            so rather than silently ignored. */}
        <span
          className={`guides-row-label${backPrinting ? "" : " is-inert"}`}
          title={backPrinting ? undefined : BACKS_OFF_TITLE}
        >
          Backs
        </span>
        <VisibilityChip
          label="Card Guides on back pages"
          shown={!values.hide_card_guides_back}
          onChange={(shown) => onChange("hide_card_guides_back", !shown)}
          disabled={!backPrinting}
        />
        <VisibilityChip
          label="Page Guides on back pages"
          shown={!values.hide_page_guides_back}
          onChange={(shown) => onChange("hide_page_guides_back", !shown)}
          disabled={!backPrinting}
        />
      </div>
    </>
  );
}
