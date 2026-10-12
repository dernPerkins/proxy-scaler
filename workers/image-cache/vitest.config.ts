import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      // The edge cache would answer repeat requests before the R2 logic
      // under test ever ran; with the Cache API off, caches.default is a
      // no-op and every request exercises the store.
      miniflare: { cacheAPI: false },
    }),
  ],
});
