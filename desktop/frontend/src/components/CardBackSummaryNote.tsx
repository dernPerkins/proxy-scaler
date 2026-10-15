// The PDF and ZIP tabs' one-line account of per-card backs: how the deck's
// Reverses split between the universal back, cards' own backs and blanks,
// with a way back to where that is changed (the Decklist rows). Silent
// when every card simply inherits — then there is nothing to account for.
//
// Also where the server-too-old warning for this feature lives: an older
// server ignores the per-card fields and prints the universal back on
// every Reverse, which is a wrong print rather than a failed one, so it
// is said out loud here before the request goes out.
import { Link } from "react-router-dom";
import type { CardBackSummary } from "../cardBacks";
import { PER_CARD_BACKS_MIN_SERVER_VERSION } from "../config";

export default function CardBackSummaryNote({
  summary,
  serverSupports,
  serverVersion,
}: {
  summary: CardBackSummary;
  /** False when a connected server predates per-card backs. */
  serverSupports: boolean;
  serverVersion: string | null;
}) {
  const own = summary.image + summary.blank;
  if (own === 0) return null;
  const parts: string[] = [];
  if (summary.image > 0) {
    const names = summary.ownBacks.map((b) => b.label).join(", ");
    parts.push(`${summary.image} use${summary.image === 1 ? "s" : ""} ${summary.ownBacks.length === 1 ? names : "their own back"}`);
  }
  if (summary.blank > 0) parts.push(`${summary.blank} print${summary.blank === 1 ? "s" : ""} blank`);
  return (
    <p className={serverSupports ? "hint" : "error-text"} style={{ margin: "2px 0 8px" }}>
      {summary.inherit} card{summary.inherit === 1 ? "" : "s"} take the universal back
      {parts.length ? `, ${parts.join(", ")}` : ""}.{" "}
      <Link to="/decklist" className="linklike">
        Review per-card backs
      </Link>
      {!serverSupports && (
        <>
          {" "}
          — the connected server predates per-card backs (needs v
          {PER_CARD_BACKS_MIN_SERVER_VERSION}
          {serverVersion ? `, it reports v${serverVersion}` : ""}) and will print the
          universal back on every one of them.
        </>
      )}
    </p>
  );
}
