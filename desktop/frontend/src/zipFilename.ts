import { defaultPdfBasename } from "./pdfFilename";

/** The name a ZIP export is saved under — the PDF convention with a
 *  different extension, sharing pdfFilename.ts's dated fallback (and its
 *  parity contract with the server's _default_pdf_basename). Same
 *  deliberate asymmetry too: a named project passes through verbatim for
 *  the save dialog while the server slugifies its Content-Disposition
 *  copy, and the save dialog wins (Tauri ignores the header). */
export function zipFilename(projectName: string | null | undefined, today?: Date): string {
  return `${projectName || defaultPdfBasename(today)}.zip`;
}
