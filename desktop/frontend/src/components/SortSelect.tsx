import { useProject } from "../context/ProjectContext";
import type { SortPrimary } from "../api/types";
import SelectMenu from "./SelectMenu";

// The one card-sort control, rendered on the Decklist, PDF, and ZIP
// pages. One component (rather than three copies of the dropdown) because
// the value is shared, persisted project state that decides both the
// Decklist display order and the printed/exported output order — the
// options must never drift between pages.
//
// The label sits outside the options ("Sort" + "Name"), not inside them
// ("Sort: Name"): the options are the values, and a caller that already
// labels the control (the PDF tab's "Card sorting" field) passes
// `label={null}` for the bare select rather than getting a second label.
export default function SortSelect({ label = "Sort" }: { label?: string | null }) {
  const { settings, setSettings } = useProject();
  const select = (
    <SelectMenu
      ariaLabel={label ?? "Card sorting"}
      options={[
        { value: "Name", label: "Name" },
        { value: "Set", label: "Set" },
        { value: "(none)", label: "(none)" },
      ]}
      value={settings.sort_primary}
      onChange={(value) => setSettings((s) => ({ ...s, sort_primary: value as SortPrimary }))}
    />
  );
  if (label == null) return select;
  return (
    <label className="sort-select">
      <span>{label}</span>
      {select}
    </label>
  );
}
