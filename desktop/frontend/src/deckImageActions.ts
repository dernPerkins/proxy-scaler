// The Decklist's whole-deck image actions — Download images and the Upscale
// button — for any set of cards, so the PDF tab can offer the very same ones
// for the cards its preview says it can't print. One implementation: the
// Decklist's buttons run through here too.
import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { generationApi } from "./api/generation";
import { projectApi, type CardRow, type CustomUpscaleMode } from "./api/project";
import type { CustomImage, GenerateResult } from "./api/types";
import { serverSupportsCustomUpscale, useServerVersion } from "./config";
import { DEFAULT_GEN_PATHS } from "./constants";
import { useProject } from "./context/ProjectContext";
import { cardToEntry } from "./deckEntries";
import { resolveImageOrigin } from "./imageOrigin";
import { fetchPendingFaceArt, hasPendingFaceArt } from "./pickDownloads";
import { getProjectSnapshot, syncCustomImages } from "./syncCustoms";
import { toast } from "./toasts";
import { SCOPE_EMPTY, scopeRequest, type UpscaleScope } from "./upscaleScope";
import { UploadCanceled } from "./uploadProgress";

/** The server's notes on an upscale request, as their own toasts beside
 *  the outcome, whether or not anything else queued: a card skipped for
 *  already reaching its DPI ("Sol Ring: already 1195 DPI.") is a warning,
 *  and a card that couldn't be prepared ("FAIL [...]") an error. */
export function toastUpscaleNotes(notes: string[]): void {
  const failures = notes.filter((n) => n.startsWith("FAIL"));
  const skips = notes.filter((n) => !n.startsWith("FAIL"));
  if (skips.length) toast(`Skipped: ${skips.join(" ")}`, "warn", { key: "generate-skips" });
  if (failures.length) toast(failures.join(" "), "err", { key: "generate-failures" });
}

export function useDeckImageActions() {
  const queryClient = useQueryClient();
  const { projectTag, settings, setCardFaceArt } = useProject();
  const serverVersion = useServerVersion();

  // How custom and MPC art comes out when an upscale button asks for it
  // (the buttons decide *whether*; this setting only decides the result).
  // A server older than the setting would drop the field anyway, so "off"
  // is sent rather than a choice it can't honour.
  const customResult: CustomUpscaleMode = serverSupportsCustomUpscale(serverVersion)
    ? settings.custom_upscale === "off"
      ? "target"
      : settings.custom_upscale
    : "off";

  // Library images that came from MPC Fill: what "Upscale MPC Fill" covers
  // among custom cards (face art says so itself).
  const libraryQuery = useQuery({
    queryKey: ["custom-images"],
    queryFn: () => projectApi.listCustomImages(),
  });
  const mpcImageIds = useMemo(
    () => new Set((libraryQuery.data ?? []).flatMap((image) => (image.mpc ? [image.id] : []))),
    [libraryQuery.data],
  );

  const invalidateStatus = () =>
    queryClient.invalidateQueries({ queryKey: ["generation-status", projectTag] });
  const refreshStatus = () => queryClient.refetchQueries({ queryKey: ["generation-status", projectTag] });

  // A pick adds its art to the library: put the image in the cached list
  // as it lands, rather than re-reading the whole library, so the row's
  // thumbnails already know the bleed to crop off.
  const setPickedFaceArt = async (cardId: number, face: number, totalFaces: number, image: CustomImage) => {
    await setCardFaceArt(cardId, face, totalFaces, image);
    queryClient.setQueryData<CustomImage[]>(["custom-images"], (list) =>
      list && !list.some((i) => i.id === image.id) ? [...list, image] : list,
    );
  };
  const pendingArtOpts = {
    setFaceArt: setPickedFaceArt,
    projectTag,
    serverVersion,
    refreshStatus,
  };

  /** Fetch these cards' chosen MPC art, then the cards as they now are. */
  async function withPendingArtFetched(subset: CardRow[]): Promise<CardRow[]> {
    if (!hasPendingFaceArt(subset)) return subset;
    await fetchPendingFaceArt(subset, pendingArtOpts);
    const ids = new Set(subset.map((c) => c.id));
    return getProjectSnapshot().cards.filter((c) => ids.has(c.id));
  }

  /** Download images: the ~300 DPI originals, no upscaling. */
  const download = useMutation({
    mutationFn: async (cards: CardRow[]) => {
      // Chosen MPC Fill art downloads in the background queue, each face
      // registering itself as it lands (the rest needn't wait for it).
      void fetchPendingFaceArt(cards, pendingArtOpts);
      // Custom cards have no Scryfall original to download; this is what
      // registers their uploaded art as printable instead, so they still
      // need syncing first.
      await syncCustomImages(cards, serverVersion);
      return generationApi.downloadOriginals({
        project_tag: projectTag as string,
        entries: cards.map(cardToEntry),
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
        image_origin: await resolveImageOrigin(queryClient),
      });
    },
    onSuccess: (result) => {
      if (result.queued) {
        toast(`Queued ${result.queued} download(s). The Tasks tab shows their progress.`, "ok", {
          key: "download",
        });
      } else {
        toast(
          `Nothing to do: every original is already downloaded.${result.notes.length ? " " + result.notes.join(" ") : ""}`,
          "info",
          { key: "download" },
        );
      }
      invalidateStatus();
    },
    onError: (err: Error) => {
      // Cancelling the custom-image upload is a normal outcome, not a failure.
      if (err instanceof UploadCanceled) return;
      toast(`Download failed: ${err.message}`, "err", { key: "download" });
    },
  });

  /** The Upscale button: these cards' art of one kind, to the selected DPIs.
   *  Resolves null when the scope covers none of them. */
  const upscale = useMutation({
    mutationFn: async ({ cards, scope }: { cards: CardRow[]; scope: UpscaleScope }): Promise<GenerateResult | null> => {
      const first = scopeRequest(cards, scope, mpcImageIds);
      if (first.entries.length === 0) return null;
      const ready = await withPendingArtFetched(first.cards);
      const request = scopeRequest(ready, scope, mpcImageIds);
      if (request.entries.length === 0) return null;
      // Custom art has to be on the server before the tasks that will read
      // it are queued — see syncCustoms.ts.
      await syncCustomImages(request.cards, serverVersion);
      return generationApi.generate({
        project_tag: projectTag as string,
        entries: request.entries,
        model: settings.model,
        dpi_targets: settings.dpi_targets,
        skip_existing: settings.skip_existing,
        tile_size: settings.tile_size,
        custom_upscale: request.upscaleCustoms ? customResult : "off",
        scryfall_upscale: request.scryfallUpscale,
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
        image_origin: await resolveImageOrigin(queryClient),
      });
    },
    onSuccess: (result, { scope }) => {
      if (result == null) toast(SCOPE_EMPTY[scope], "info", { key: "generate" });
      else {
        toast(
          result.queued
            ? `Queued ${result.queued} task(s). The Tasks tab shows their progress.`
            : "Nothing to do: every requested image already exists.",
          result.queued ? "ok" : "info",
          { key: "generate" },
        );
        toastUpscaleNotes(result.notes);
      }
      invalidateStatus();
    },
    onError: (err: Error) => {
      // Cancelling the custom-image upload is a normal outcome, not a failure.
      if (err instanceof UploadCanceled) return;
      toast(`Upscale failed: ${err.message}`, "err", { key: "generate" });
    },
  });

  return { download, upscale, customResult, mpcImageIds, pendingArtOpts, setPickedFaceArt, withPendingArtFetched };
}
