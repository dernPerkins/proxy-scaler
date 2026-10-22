// The "Add …" panel at the top of the Customs and Backs sidebars: a small
// dropzone that opens the file picker, plus whatever else adds to that
// library (the Backs tab's MPC Fill search). It sits in the sidebar so the
// library grid holds only the library. Chosen from a prototype
// (.scratch/library-add-prototype/).
//
// Dropping works anywhere on the tab's pane, not only on this zone: the
// page owns the drag handlers and passes `dragging` in, so the zone lights
// up for a drop that will land.
import { useRef, type ReactNode } from "react";
import { MAX_UPLOAD_MB } from "../imageUpload";

/* Inline rather than an asset or an icon package: one drawing, and
   `currentColor` lets it follow the dropzone's hover/drag state in both
   themes. */
function UploadIcon() {
  return (
    <svg
      className="dropzone-icon"
      width="28"
      height="28"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M20.4 14.5A4 4 0 0 0 18 7.5h-1.3A6 6 0 1 0 6 12.9" />
      <path d="M12 12v9" />
      <path d="m8.5 15.5 3.5-3.5 3.5 3.5" />
    </svg>
  );
}

export default function LibraryAddPanel(props: {
  title: string;
  accept: string;
  multiple?: boolean;
  dragging: boolean;
  /** Shown in place of the prompt while files are being added. */
  busyLabel: string | null;
  /** The dropzone's tour anchor. */
  tour: string;
  onFiles: (files: File[]) => void;
  /** More ways to add, under the dropzone. */
  children?: ReactNode;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  return (
    <aside className="sidebar panel library-add">
      <h3 style={{ marginBottom: 10 }}>{props.title}</h3>
      <button
        type="button"
        className={`dropzone library-add-drop${props.dragging ? " is-dragging" : ""}`}
        data-tour={props.tour}
        disabled={props.busyLabel != null}
        onClick={() => fileInput.current?.click()}
      >
        <UploadIcon />
        <span className="dropzone-title">
          {props.busyLabel ?? "Drop an image or click to upload"}
        </span>
        <span className="dropzone-hint">
          PNG, JPEG or WebP, up to {MAX_UPLOAD_MB}MB{props.multiple ? " each" : ""}
        </span>
      </button>
      {/* Beside the button that clicks it, so the two can't be separated —
          an input moved away once silently made the zone do nothing. */}
      <input
        ref={fileInput}
        type="file"
        multiple={props.multiple}
        accept={props.accept}
        style={{ display: "none" }}
        onChange={(e) => {
          const files = e.target.files ? Array.from(e.target.files) : [];
          // Reset first: picking the same file twice in a row fires no
          // change event otherwise, which reads as a broken control.
          e.target.value = "";
          if (files.length) props.onFiles(files);
        }}
      />
      {props.children}
    </aside>
  );
}
