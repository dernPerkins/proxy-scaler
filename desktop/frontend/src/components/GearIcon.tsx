// The gear drawn as an SVG rather than the ⚙ glyph, which webviews draw
// at different, and visibly smaller, sizes than the text around it.
// Shared by the tab bar's Settings pill and the preset bar's Manage button.
export default function GearIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="-2.35 -2.35 28.7 28.7" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M19.76 8.79 L20.25 10.40 L22.91 10.56 L22.91 13.44 L20.25 13.60 L19.76 15.21 L18.96 16.70 L20.73 18.70 L18.70 20.73 L16.70 18.96 L15.21 19.76 L13.60 20.25 L13.44 22.91 L10.56 22.91 L10.40 20.25 L8.79 19.76 L7.30 18.96 L5.30 20.73 L3.27 18.70 L5.04 16.70 L4.24 15.21 L3.75 13.60 L1.09 13.44 L1.09 10.56 L3.75 10.40 L4.24 8.79 L5.04 7.30 L3.27 5.30 L5.30 3.27 L7.30 5.04 L8.79 4.24 L10.40 3.75 L10.56 1.09 L13.44 1.09 L13.60 3.75 L15.21 4.24 L16.70 5.04 L18.70 3.27 L20.73 5.30 L18.96 7.30Z M15.4 12 A3.4 3.4 0 1 0 8.6 12 A3.4 3.4 0 1 0 15.4 12Z" />
    </svg>
  );
}
