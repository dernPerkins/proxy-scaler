import { invokeSaveFile, isTauri } from "./tauri";

// Confirmed by real testing (not a theoretical webview quirk): the HTML
// `download` attribute is not reliably honored by Tauri's webview on
// macOS (WKWebView) — a static <a download> link to an image just
// navigated to a larger view of the image instead of saving it, and a
// blob-URL <a download> link (the usual JS-driven download trick) for a
// generated PDF did nothing at all. Inside Tauri this goes through a
// native "Save As" dialog + Rust-side std::fs::write instead (see
// tauri.ts::invokeSaveFile / main.rs::save_file). In a plain browser tab
// (e.g. `npm run dev` without Tauri) the standard anchor+download trick
// works fine, so that path is kept as the fallback.
export async function downloadBlob(blob: Blob, filename: string): Promise<void> {
  if (isTauri()) {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    await invokeSaveFile(filename, bytes);
    return;
  }

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
