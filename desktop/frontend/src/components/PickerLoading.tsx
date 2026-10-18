// What the image-grid pickers show while their results load: the Scryfall
// printings, or an MPC Fill search (the first one can take a moment, as
// the server asks mpcfill.com).
//
// The preview column becomes the preview pane's own dark stage — a card
// silhouette breathing on a slowly drifting wash, with an activity spinner
// and the label on its scrim — and the grid fills with shimmering
// placeholder tiles in the shape of the results to come. Chosen from a
// prototype (.scratch/mpcfill-art-picker/prototype-picker-loading/).

/** The preview column (grid column 1 of .printing-grid-modal). */
export function LoadingStage({ label }: { label: string }) {
  return (
    <aside className="printing-preview loading-stage" aria-label={label} aria-busy="true">
      <div className="loading-stage-wash" />
      <div className="printing-preview-stage">
        <div className="loading-stage-card" />
      </div>
      <div className="printing-preview-scrim loading-stage-scrim">
        <ActivitySpinner />
        <span className="loading-stage-label">{label}</span>
      </div>
    </aside>
  );
}

/** Placeholder tiles for the scrolling results area. */
export function LoadingGrid({ label }: { label: string }) {
  return (
    <div className="printing-grid loading-grid" aria-label={label} aria-busy="true">
      {Array.from({ length: 18 }, (_, i) => (
        <div key={i} className="loading-tile" style={{ animationDelay: `${(i % 6) * 60}ms` }}>
          <div className="loading-tile-frame loading-shimmer" />
          <div
            className="loading-line loading-shimmer"
            style={{ width: `${55 + ((i * 17) % 35)}%` }}
          />
        </div>
      ))}
    </div>
  );
}

/** A macOS-style activity indicator: twelve fading spokes. */
function ActivitySpinner() {
  return (
    <span className="activity-spinner" aria-hidden="true">
      {Array.from({ length: 12 }, (_, i) => (
        <i
          key={i}
          style={{ transform: `rotate(${i * 30}deg)`, animationDelay: `${(i - 12) / 12}s` }}
        />
      ))}
    </span>
  );
}
