// The Decklist tab's card view: the Grid (a page preview), or rows.
// Remembered app-wide rather than per project, like the Add Cards mode
// (AddCards.tsx's `useAddMode`) — how a deck is *looked at* is the user's
// habit, not something about what gets printed, so a project keeps its
// print settings untouched by it.
//
// An absent store reads as the Grid view (project_store.rs: nothing saved
// yet means a new or upgrading user), which is also what a plain browser
// dev tab (no Tauri, no store) gets. After that it's the view used last.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectApi, type DeckViewMode } from "./api/project";
import { isTauri } from "./tauri";

const deckViewModeQuery = {
  queryKey: ["deck-view-mode"],
  queryFn: () =>
    isTauri()
      ? projectApi.getDeckViewMode().catch(() => "grid" as DeckViewMode)
      : "grid" as DeckViewMode,
  staleTime: Infinity,
};

export function useDeckViewMode(): {
  mode: DeckViewMode;
  setMode: (mode: DeckViewMode) => void;
} {
  const queryClient = useQueryClient();
  const saved = useQuery(deckViewModeQuery);
  const save = useMutation({
    mutationFn: (mode: DeckViewMode) =>
      isTauri() ? projectApi.setDeckViewMode(mode) : Promise.resolve(),
    // The write lands in the query the switch already moved.
    onSuccess: (_data, mode) => queryClient.setQueryData(deckViewModeQuery.queryKey, mode),
    // The write failed: show what is actually stored again.
    onError: () => queryClient.invalidateQueries({ queryKey: deckViewModeQuery.queryKey }),
  });
  return {
    mode: saved.data ?? "grid",
    setMode: (mode) => {
      // The view flips at once; the store catches up (or falls back on
      // onError if it can't).
      queryClient.setQueryData(deckViewModeQuery.queryKey, mode);
      save.mutate(mode);
    },
  };
}
