// Selectable list of previously-used remote server addresses, shown below
// the address field in both ConnectGate.tsx (startup picker) and
// SwitchServerDialog.tsx (mid-session switch) — see connection.tsx's
// recentHosts/removeRecentHost. The two callers deliberately use `onSelect`
// differently (immediate-connect vs. fill-only + highlight); this component
// just renders the list and reports clicks, it doesn't know which.
export interface RecentHostsListProps {
  hosts: string[];
  onSelect: (host: string) => void;
  onRemove: (host: string) => void;
  /** Highlighted green as the current pick — e.g. SwitchServerDialog.tsx
   *  passes its own `host` state here, so clicking a row gives clear
   *  feedback of *what* got selected even though nothing else on screen
   *  changes yet (a confirm button still has to be clicked separately). */
  selectedHost?: string;
  disabled?: boolean;
}

export default function RecentHostsList({
  hosts,
  onSelect,
  onRemove,
  selectedHost,
  disabled,
}: RecentHostsListProps) {
  if (hosts.length === 0) return null;

  return (
    <div className="host-list">
      {hosts.map((host) => (
        <div key={host} className="host-row">
          <button
            className={"host-row-select mono" + (host === selectedHost ? " selected" : "")}
            onClick={() => onSelect(host)}
            disabled={disabled}
            title={`Connect to ${host}`}
          >
            {host}
          </button>
          <button
            className="btn-sm btn-danger"
            onClick={() => onRemove(host)}
            disabled={disabled}
            title={`Remove ${host} from saved servers`}
          >
            Delete
          </button>
        </div>
      ))}
    </div>
  );
}
