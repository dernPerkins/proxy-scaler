import { useDecklistPanels } from "../decklistPanels";
import { shortcutLabel } from "../shortcuts";

// The clickable side of Cmd/Ctrl+B and +I: two round pills in the tab
// bar's right-hand cluster, left of the Tutorial ?, styled like it and the
// Settings gear (an icon that slides open to its label on hover or focus).
// Shown only on the Decklist tab, the one with panels to hide.
//
// Each icon is a window with the panel drawn in: filled while the panel
// shows, an empty outline while it's hidden, so the pill reads the state
// at a glance without opening.
export default function DecklistPanelButtons() {
  const panels = useDecklistPanels();
  // One box around both, for the Decklist tour's step on them (tours.ts).
  return (
    <span className="panel-buttons" data-tour="panel-buttons">
      <PanelButton
        label={panels.sidebarHidden ? "Show Sidebar" : "Hide Sidebar"}
        shortcut={shortcutLabel("B")}
        shown={!panels.sidebarHidden}
        onClick={panels.toggleSidebar}
        icon={<SidebarIcon shown={!panels.sidebarHidden} />}
      />
      <PanelButton
        label={panels.addCardsHidden ? "Show Add Cards" : "Hide Add Cards"}
        shortcut={shortcutLabel("I")}
        shown={!panels.addCardsHidden}
        onClick={panels.toggleAddCards}
        icon={<TopPanelIcon shown={!panels.addCardsHidden} />}
      />
    </span>
  );
}

function PanelButton({
  label,
  shortcut,
  shown,
  onClick,
  icon,
}: {
  label: string;
  shortcut: string;
  shown: boolean;
  onClick: () => void;
  icon: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className="reveal-btn reveal-btn-wide"
      aria-label={label}
      aria-pressed={shown}
      title={shortcut}
      onClick={onClick}
    >
      <span className="reveal-btn-icon" aria-hidden="true">
        {icon}
      </span>
      <span className="reveal-btn-label">{label}</span>
    </button>
  );
}

// Drawn in the same 12px box as the ? and the gear (.reveal-btn-svg).
function SidebarIcon({ shown }: { shown: boolean }) {
  return (
    <svg className="reveal-btn-svg" viewBox="0 0 12 12" aria-hidden="true">
      <rect x="0.75" y="1.25" width="10.5" height="9.5" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
      {shown && <rect x="1.4" y="1.9" width="3.1" height="8.2" rx="1.2" fill="currentColor" />}
      <line x1="4.75" y1="1.25" x2="4.75" y2="10.75" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}

function TopPanelIcon({ shown }: { shown: boolean }) {
  return (
    <svg className="reveal-btn-svg" viewBox="0 0 12 12" aria-hidden="true">
      <rect x="0.75" y="1.25" width="10.5" height="9.5" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
      {shown && <rect x="1.4" y="1.9" width="9.2" height="2.6" rx="1.2" fill="currentColor" />}
      <line x1="0.75" y1="4.75" x2="11.25" y2="4.75" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}
