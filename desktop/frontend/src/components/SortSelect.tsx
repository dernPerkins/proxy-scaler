import { useProject } from "../context/ProjectContext";
import type { SortPrimary } from "../api/types";

// The one card-sort control, rendered on the Decklist, PDF, and Export
// pages. One component (rather than three copies of the <select>) because
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
    <select
      value={settings.sort_primary}
      onChange={(e) =>
        setSettings((s) => ({ ...s, sort_primary: e.target.value as SortPrimary }))
      }
    >
      <option value="Name">Name</option>
      <option value="Set">Set</option>
      <option value="(none)">(none)</option>
    </select>
  );
  if (label == null) return select;
  return (
    <label className="sort-select">
      <span>{label}</span>
      {select}
    </label>
  );
}
