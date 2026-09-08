import { useProject } from "../context/ProjectContext";
import type { SortPrimary } from "../api/types";

// The one card-sort control, rendered on the Decklist, PDF, and Export
// pages. One component (rather than three copies of the <select>) because
// the value is shared, persisted project state that decides both the
// Decklist display order and the printed/exported output order — the
// options must never drift between pages.
export default function SortSelect() {
  const { settings, setSettings } = useProject();
  return (
    <select
      value={settings.sort_primary}
      onChange={(e) =>
        setSettings((s) => ({ ...s, sort_primary: e.target.value as SortPrimary }))
      }
    >
      <option value="Name">Sort: Name</option>
      <option value="Set">Sort: Set</option>
      <option value="(none)">Sort: (none)</option>
    </select>
  );
}
