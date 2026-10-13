// App Settings' "Use Proxy Scaler Image Cache (Recommended)". On, the
// generation server fetches card images through the Proxy Scaler image
// cache (a Cloudflare Worker that falls back to Scryfall by itself); off,
// straight from Scryfall, with the Worker never contacted. The choice is
// this client's, so it rides on every request that can fetch an image — the
// Generate-family bodies' `image_origin` and the thumbnail URL's `via` —
// and in Remote mode it is the user's, not the server operator's. See
// docs/adr/0005-image-fetches-via-cloudflare-worker-cache.md.
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { projectApi } from "./api/project";
import type { ImageOrigin } from "./api/types";
import { isTauri } from "./tauri";

const imageCacheQuery = {
  queryKey: ["image-cache-enabled"],
  // A plain browser dev tab has no settings store; the default holds.
  // Unreadable means on, matching the Rust default for an absent key.
  queryFn: () =>
    isTauri() ? projectApi.getImageCacheEnabled().catch(() => true) : Promise.resolve(true),
  staleTime: Infinity,
};

const toOrigin = (enabled: boolean): ImageOrigin => (enabled ? "worker" : "scryfall");

/** The origin to put in a request body. Awaits the stored setting, so a
 *  request sent before it first loads can't default to the Worker for a
 *  user who switched it off. */
export async function resolveImageOrigin(queryClient: QueryClient): Promise<ImageOrigin> {
  return toOrigin(await queryClient.ensureQueryData(imageCacheQuery));
}

export function useImageCacheSetting(): {
  /** null until the stored setting has loaded. */
  enabled: boolean | null;
  origin: ImageOrigin | null;
  setEnabled: (enabled: boolean) => void;
} {
  const queryClient = useQueryClient();
  const query = useQuery(imageCacheQuery);
  const mutation = useMutation({
    mutationFn: (enabled: boolean) => projectApi.setImageCacheEnabled(enabled),
    onMutate: (enabled) => queryClient.setQueryData(imageCacheQuery.queryKey, enabled),
    // The write failed: show what is actually stored again.
    onError: () => queryClient.invalidateQueries({ queryKey: imageCacheQuery.queryKey }),
  });
  const enabled = query.data ?? null;
  return {
    enabled,
    origin: enabled == null ? null : toOrigin(enabled),
    setEnabled: (next) => mutation.mutate(next),
  };
}
