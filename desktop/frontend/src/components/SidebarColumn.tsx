import type { ReactNode } from "react";

/** The column a tab's sidebar sits in: sticky under the pinned tab bar,
 *  and scrolled on its own so the whole sidebar slides past the window's
 *  edge rather than scrolling inside itself. See styles.css. */
export default function SidebarColumn({ children }: { children: ReactNode }) {
  return <div className="sidebar-column">{children}</div>;
}
