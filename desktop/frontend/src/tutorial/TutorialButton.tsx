import type { TourId } from "./tours";
import { replayTour } from "./tutorialStore";

// The way back into a tour: a round ? that slides open to read
// "Tutorial" on hover or keyboard focus, so it stays out of the way until
// someone goes looking for help. Tours also point at it (data-tour) as
// their "replay this any time" step.
export default function TutorialButton({
  tour,
  className,
}: {
  tour: TourId;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={className ? `tutorial-btn ${className}` : "tutorial-btn"}
      aria-label="Show tutorial"
      data-tour="tutorial-button"
      onClick={() => replayTour(tour)}
    >
      <span className="tutorial-btn-icon" aria-hidden="true">
        ?
      </span>
      <span className="tutorial-btn-label">Tutorial</span>
    </button>
  );
}
