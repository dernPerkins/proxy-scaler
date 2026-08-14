/**
 * The name a PDF export is saved under. Its own module (rather than
 * inlined at the call site) so the fallback can be exercised directly and
 * kept honest against the server's copy in
 * proxy_scaler/api/routers/pdf.py::_default_pdf_basename — this string is
 * only the save-dialog default, while the same name comes back in the
 * download's Content-Disposition, and the two used to disagree.
 *
 * Parity is claimed for the *fallback* only. A named project is passed
 * through verbatim here while the server runs it through _slugify, so
 * "My Deck" offers "My Deck.pdf" and the header says "My_Deck.pdf" —
 * long-standing behaviour, left alone deliberately: the friendlier name
 * is the right one to show in a save dialog.
 */

/** Local calendar date as YYYY-MM-DD.
 *
 *  Deliberately not toISOString().slice(0, 10): that's UTC, so it names
 *  the file with the wrong day for anyone whose local date differs from
 *  UTC's at the moment they export. The server's date.today() is local
 *  to *its* machine, which for a remote generation host can be a
 *  different day again — the saved file always takes this name (Tauri
 *  downloads to the chosen path and ignores Content-Disposition), so
 *  local-here is the one to be right about. */
function isoLocalDate(today: Date): string {
  const month = String(today.getMonth() + 1).padStart(2, "0");
  const day = String(today.getDate()).padStart(2, "0");
  return `${today.getFullYear()}-${month}-${day}`;
}

/** Filename stem used when the project has no name (an Unnamed Project).
 *  Never the project_tag — an opaque 32-char hex string is no name to
 *  offer someone saving a file. */
export function defaultPdfBasename(today: Date = new Date()): string {
  return `proxy-scaler-${isoLocalDate(today)}`;
}

export function pdfFilename(projectName: string | null | undefined, today?: Date): string {
  return `${projectName || defaultPdfBasename(today)}.pdf`;
}
