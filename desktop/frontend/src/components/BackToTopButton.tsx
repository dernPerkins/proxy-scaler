// A round "back to top" button in the bottom-right corner, shown once the
// window has scrolled well down a long page (the MPC Fill tab's library).
// Chosen from a prototype (.scratch/mpcfill-art-picker/prototype-back-to-top/).
//
// It shares that corner with the toast stack, so while it is showing it
// sets `has-back-to-top` on <body> and the stack moves up above it
// (styles.css) rather than toasts landing on top of the button.
import { useEffect, useState } from "react";

const SHOW_AFTER_PX = 500;

export default function BackToTopButton() {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const onScroll = () => setShown(window.scrollY > SHOW_AFTER_PX);
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    document.body.classList.toggle("has-back-to-top", shown);
    return () => document.body.classList.remove("has-back-to-top");
  }, [shown]);

  if (!shown) return null;
  return (
    <button
      type="button"
      className="back-to-top"
      title="Back to top"
      aria-label="Back to top"
      onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
    >
      ↑
    </button>
  );
}
