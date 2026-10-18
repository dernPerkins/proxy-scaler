// The Decklist card row's action icons (Show/Hide, Download, Generate,
// Remove). Line icons on a 24px grid in currentColor, matching
// ImageViewer's MagnifierIcon; each button carries its own tooltip.
const common = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

/** Show this card's images. */
export function EyeIcon() {
  return (
    <svg {...common}>
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

/** Hide this card's images. */
export function EyeOffIcon() {
  return (
    <svg {...common}>
      <path d="M10.6 5.1A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a18 18 0 0 1-3.1 4" />
      <path d="M6.6 6.6C3.7 8.5 2 12 2 12s3.6 7 10 7a9.8 9.8 0 0 0 5.4-1.6" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
      <path d="M3 3l18 18" />
    </svg>
  );
}

/** Download the full-size original(s), no upscaling. */
export function DownloadIcon() {
  return (
    <svg {...common}>
      <path d="M12 4v11" />
      <path d="M7 10l5 5 5-5" />
      <path d="M5 20h14" />
    </svg>
  );
}

/** Generate: upscale to the selected DPIs. */
export function SparklesIcon() {
  return (
    <svg {...common}>
      <path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z" />
      <path d="M19 15l.8 2.2 2.2.8-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z" />
    </svg>
  );
}

/** Remove the card from the deck. */
export function TrashIcon() {
  return (
    <svg {...common}>
      <path d="M4 7h16" />
      <path d="M10 11v6M14 11v6" />
      <path d="M6 7l1 13h10l1-13" />
      <path d="M9 7V4h6v3" />
    </svg>
  );
}
