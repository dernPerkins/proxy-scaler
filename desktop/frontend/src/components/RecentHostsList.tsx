import type { RecentHost } from "../api/project";

// Selectable list of previously-used remote server address+port pairs,
// shown below the address field in both ConnectGate.tsx (startup picker)
// and SwitchServerDialog.tsx (mid-session switch) — see connection.tsx's
// recentHosts/removeRecentHost. The two callers deliberately use `onSelect`
// differently (immediate-connect vs. fill-only + highlight); this component
// just renders the list and reports clicks, it doesn't know which.
export interface RecentHostsListProps {
  hosts: RecentHost[];
  onSelect: (entry: RecentHost) => void;
  onRemove: (entry: RecentHost) => void;
  /** Highlighted green as the current pick — e.g. SwitchServerDialog.tsx
   *  passes its own host/port state here, so clicking a row gives clear
   *  feedback of *what* got selected even though nothing else on screen
   *  changes yet (a confirm button still has to be clicked separately). */
  selected?: RecentHost;
  disabled?: boolean;
}

export default function RecentHostsList({
  hosts,
  onSelect,
  onRemove,
  selected,
  disabled,
}: RecentHostsListProps) {
  if (hosts.length === 0) return null;

  return (
    <div className="host-list">
      {hosts.map((entry) => {
        const label = `${entry.host}:${entry.port}`;
        const isSelected = selected != null && selected.host === entry.host && selected.port === entry.port;
        return (
          <div key={label} className="host-row">
            <button
              className={"host-row-select mono" + (isSelected ? " selected" : "")}
              onClick={() => onSelect(entry)}
              disabled={disabled}
              title={`Connect to ${label}`}
            >
              {label}
            </button>
            <button
              className="btn-sm btn-danger"
              onClick={() => onRemove(entry)}
              disabled={disabled}
              title={`Remove ${label} from saved servers`}
            >
              Delete
            </button>
          </div>
        );
      })}
    </div>
  );
}
