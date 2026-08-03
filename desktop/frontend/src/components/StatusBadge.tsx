import type { ReactNode } from "react";

// Shared by DecklistPage (per-variant) and TasksPage (per-task), which
// previously kept their own duplicate emoji-per-status maps that could
// drift apart. A colored dot carries the status; `children` carries
// whatever label makes sense in context (a dpi·model pair on a card row,
// the status word itself in the tasks table), with the raw status always
// available on hover for the cases where color alone is ambiguous.
export default function StatusBadge({
  status,
  children,
}: {
  status: string;
  children?: ReactNode;
}) {
  return (
    <span className={`badge badge-${status}`} title={status}>
      <span className="dot" />
      {children ?? status}
    </span>
  );
}
