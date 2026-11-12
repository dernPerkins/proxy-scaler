// The Decklist's two hideable panels: the sidebar column (Cmd/Ctrl+B) and
// the Add Cards box above the deck (Cmd/Ctrl+I). Remembered app-wide like
// the Row/Grid view (deckViewMode.ts) — how the tab is laid out is the
// user's habit, nothing about what prints.
//
// An absent store reads as both shown, which is also what a plain browser
// dev tab (no Tauri, no store) gets.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectApi, type DecklistPanel, type DecklistPanels } from "./api/project";
import { isTauri } from "./tauri";

const SHOWN: DecklistPanels = { sidebar_hidden: false, add_cards_hidden: false };

const decklistPanelsQuery = {
  queryKey: ["decklist-panels"],
  queryFn: () =>
    isTauri() ? projectApi.getDecklistPanels().catch(() => SHOWN) : SHOWN,
  staleTime: Infinity,
};

export function useDecklistPanels(): {
  sidebarHidden: boolean;
  addCardsHidden: boolean;
  /** True once the user has toggled a panel on this visit to the tab:
   *  until then the saved state (loading in at launch) lands without a
   *  slide. */
  animate: boolean;
  toggleSidebar: () => void;
  toggleAddCards: () => void;
} {
  const queryClient = useQueryClient();
  const saved = useQuery(decklistPanelsQuery);
  const panels = saved.data ?? SHOWN;
  const [animate, setAnimate] = useState(false);
  const save = useMutation({
    mutationFn: ({ panel, hidden }: { panel: DecklistPanel; hidden: boolean }) =>
      isTauri() ? projectApi.setDecklistPanelHidden(panel, hidden) : Promise.resolve(),
    // The write failed: show what is actually stored again.
    onError: () => queryClient.invalidateQueries({ queryKey: decklistPanelsQuery.queryKey }),
  });
  const toggle = (panel: DecklistPanel) => {
    const field = panel === "sidebar" ? "sidebar_hidden" : "add_cards_hidden";
    // Read from the cache, not the render's `panels`: two presses inside
    // one render would otherwise both flip the same stale value.
    const current = queryClient.getQueryData<DecklistPanels>(decklistPanelsQuery.queryKey) ?? SHOWN;
    const hidden = !current[field];
    setAnimate(true);
    // The panel moves at once; the store catches up (or falls back on
    // onError if it can't).
    queryClient.setQueryData(decklistPanelsQuery.queryKey, { ...current, [field]: hidden });
    save.mutate({ panel, hidden });
  };
  return {
    sidebarHidden: panels.sidebar_hidden,
    addCardsHidden: panels.add_cards_hidden,
    animate,
    toggleSidebar: () => toggle("sidebar"),
    toggleAddCards: () => toggle("add_cards"),
  };
}
