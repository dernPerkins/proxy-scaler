import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      // Tests never touch the real bucket, even through a binding that
      // wrangler.jsonc marks remote.
      remoteBindings: false,
      miniflare: {
        // A fake key, no pauses between mocked Drive requests or before
        // retries, a short request timeout, and two folders per crawl
        // chunk so even small test trees span several chunks.
        bindings: {
          GOOGLE_API_KEY: "test-key",
          DRIVE_GAP_MS: "0",
          DRIVE_BACKOFF_MS: "0",
          DRIVE_TIMEOUT_MS: "200",
          CRAWL_CHUNK_FOLDERS: "2",
        },
      },
    }),
  ],
});
