// A set's symbol, from the generation server's cache of Scryfall's set SVGs
// (GET /api/sets/{code}/icon). The SVG is used as a CSS mask over a fill,
// not as an <img>: Scryfall's icons are flat black, which would vanish on
// the dark theme, so the shape comes from the SVG and the color from
// styles.css (rarity gradients, else the surrounding text color). A mask
// that fails to load renders nothing, so an unknown set or an offline
// cache miss just leaves no icon. Decorative: the set code always sits
// next to it.
import type { CSSProperties } from "react";
import { generationApi } from "../api/generation";
import { serverSupportsSetIcons, useServerVersion } from "../config";

export default function SetIcon(props: {
  setCode: string | null | undefined;
  /** Scryfall rarity; colors the symbol. Absent or unknown keeps the
   *  text color, which is also what a set (rather than a card) gets. */
  rarity?: string | null;
}) {
  const { setCode, rarity } = props;
  const serverVersion = useServerVersion();
  if (!setCode || !serverSupportsSetIcons(serverVersion)) return null;
  const style = {
    "--set-icon": `url("${generationApi.setIconUrl(setCode)}")`,
  } as CSSProperties;
  return (
    <span
      className="set-icon"
      data-rarity={rarity ?? undefined}
      style={style}
      aria-hidden="true"
    />
  );
}
