// The per-card back control on a Decklist row: a card-shaped swatch
// showing what this card's Reverse will take, and the picker it opens.
//
// Same shape as PrintingPicker — a trigger in the row, a modal mounted
// locally while open — because it answers the same kind of question about
// one card ("which image?") and the row's alignment rules are the same.
//
// What the swatch shows is the RESOLVED answer, not the stored one: a card
// whose transform side prints on its back is shown locked even if a back
// was chosen for it, because that is what the paper will say. The stored
// choice is kept (the picker says so) and applies again the moment the
// PDF-tab pairing setting goes off. Resolution order, mirrored from
// pdf_layout.resolve_reverse: transform face > this card's own mode > the
// project's reverse_fill + universal back.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import type { CardRow } from "../api/project";
import { projectApi } from "../api/project";
import type { BackImage, BackMode } from "../api/types";
import { useProject } from "../context/ProjectContext";
import { useBackLibrary } from "../cardBacks";
import { ACCEPTED_IMAGE_TYPES, MAX_UPLOAD_MB, readPickedImage } from "../imageUpload";
import FullImageDialog from "./FullImageDialog";
import { MagnifierIcon } from "./ImageViewer";
import ModalOverlay from "./ModalOverlay";

/* Inline, currentColor, one place each — same reasoning as BacksPage's
   UploadIcon. Three glyphs: "follows the project", "nothing", "flips". */
function LinkIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.5 1.5" />
      <path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7L12 19" />
    </svg>
  );
}
function BlankIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="m5.5 5.5 13 13" />
    </svg>
  );
}
function FlipIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M7 16V4m0 0L3 8m4-4 4 4" />
      <path d="M17 8v12m0 0 4-4m-4 4-4-4" />
    </svg>
  );
}
function QuestionIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 1-1 1.7v.5" />
      <circle cx="12" cy="17.5" r=".6" />
    </svg>
  );
}

function useBackThumb(id: number | null | undefined) {
  return useQuery({
    queryKey: ["back-thumb", id],
    queryFn: () => projectApi.backImageThumbnail(id as number),
    enabled: id != null,
    staleTime: Infinity,
  });
}

/** What the row swatch has to say, derived once so the swatch, its
 *  tooltip and the picker's header all agree. */
type Resolved =
  | { kind: "locked" }
  | { kind: "blank" }
  | { kind: "image"; back: BackImage }
  | { kind: "universal"; back: BackImage | undefined };

function resolve(
  card: CardRow,
  library: BackImage[] | undefined,
  universalId: number | null,
  transformOnBack: boolean,
): Resolved {
  if (transformOnBack) return { kind: "locked" };
  if (card.back_mode === "blank") return { kind: "blank" };
  if (card.back_mode === "image" && card.back_image_id != null) {
    const back = library?.find((b) => b.id === card.back_image_id);
    // A dangling pointer is "inherit" by Rust's reading; show it that way
    // rather than an empty frame while the library query is still loading.
    if (back) return { kind: "image", back };
  }
  return {
    kind: "universal",
    back: universalId != null ? library?.find((b) => b.id === universalId) : undefined,
  };
}

export default function CardBackPicker({
  card,
  transformOnBack,
}: {
  card: CardRow;
  /** This card's transform side is what prints on its back (a generated
   *  double-faced card while the PDF tab pairs faces) — the per-card
   *  choice is saved but has nowhere to land. */
  transformOnBack: boolean;
}) {
  const { settings } = useProject();
  const libraryQuery = useBackLibrary();
  const resolved = resolve(card, libraryQuery.data, settings.back_image_id, transformOnBack);
  const thumbId =
    resolved.kind === "image" ? resolved.back.id : resolved.kind === "universal" ? resolved.back?.id : null;
  const thumbQuery = useBackThumb(thumbId);
  const [open, setOpen] = useState(false);

  const title = (() => {
    switch (resolved.kind) {
      case "locked":
        return "Back: this card's transform side. Turn off “print a double-faced card's transform side on its own back” on the PDF tab to use a back image.";
      case "blank":
        return "Back: blank — nothing prints on this card's back";
      case "image":
        return `Back: ${resolved.back.label} (this card only)`;
      case "universal":
        return resolved.back
          ? `Back: universal (${resolved.back.label}). Click to choose one for this card.`
          : "Back: universal — none selected yet. Pick one on the Backs tab, or choose one for this card.";
    }
  })();

  return (
    <span className="card-back-cell" data-tour="card-back">
      <button
        type="button"
        className={`card-back-swatch is-${resolved.kind}`}
        title={title}
        aria-label={title}
        onClick={() => setOpen(true)}
      >
        {thumbQuery.data && (resolved.kind === "image" || resolved.kind === "universal") ? (
          <img src={thumbQuery.data} alt="" className="card-back-swatch-img" />
        ) : null}
        {resolved.kind === "locked" ? (
          <FlipIcon />
        ) : resolved.kind === "blank" ? (
          <BlankIcon />
        ) : resolved.kind === "universal" && !thumbQuery.data ? (
          resolved.back ? null : <QuestionIcon />
        ) : null}
      </button>
      {open && (
        <CardBackModal
          card={card}
          transformOnBack={transformOnBack}
          library={libraryQuery.data ?? []}
          universal={settings.back_image_id != null ? libraryQuery.data?.find((b) => b.id === settings.back_image_id) : undefined}
          onClose={() => setOpen(false)}
        />
      )}
    </span>
  );
}

function CardBackTile({
  selected,
  onSelect,
  onPickAndClose,
  onView,
  art,
  label,
  sub,
  badge,
  tour,
}: {
  selected: boolean;
  /** Single click / Space: pick and stay open, so backs can be compared. */
  onSelect: () => void;
  /** Double click / Enter: pick and close — the common case once the
   *  user knows which one they want. */
  onPickAndClose: () => void;
  onView?: () => void;
  art: ReactNode;
  label: string;
  sub?: string;
  badge?: string;
  tour?: string;
}) {
  return (
    // A div acting as a button, as BackTile is: the zoom inside is itself
    // a button, and buttons cannot nest.
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      className={`card-back-tile${selected ? " is-selected" : ""}`}
      data-tour={tour}
      onClick={onSelect}
      onDoubleClick={onPickAndClose}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          onPickAndClose();
        } else if (e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="card-back-tile-art">
        {art}
        {badge && <span className="chip card-back-tile-badge">{badge}</span>}
        {onView && (
          <button
            type="button"
            className="thumb-zoom"
            title="View full image"
            aria-label="View full image"
            onClick={(e) => {
              // Viewing must not also pick.
              e.stopPropagation();
              onView();
            }}
            onDoubleClick={(e) => e.stopPropagation()}
          >
            <MagnifierIcon />
          </button>
        )}
      </div>
      <div className="card-back-tile-label" title={label}>
        {label}
      </div>
      {sub && <div className="card-back-tile-sub">{sub}</div>}
    </div>
  );
}

function LibraryTileArt({ back }: { back: BackImage }) {
  const thumb = useBackThumb(back.id);
  return thumb.data ? <img src={thumb.data} alt={back.label} /> : null;
}

function CardBackModal({
  card,
  transformOnBack,
  library,
  universal,
  onClose,
}: {
  card: CardRow;
  transformOnBack: boolean;
  library: BackImage[];
  universal: BackImage | undefined;
  onClose: () => void;
}) {
  const { cards, setCardBack } = useProject();
  const queryClient = useQueryClient();
  const [viewTarget, setViewTarget] = useState<{ title: string; src: string } | null>(null);
  const viewOpen = useRef(false);
  viewOpen.current = viewTarget != null;
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      // The full-image view has its own Esc; one press closes one layer.
      if (e.key === "Escape" && !viewOpen.current) onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const pickMutation = useMutation({
    mutationFn: (args: { mode: BackMode; id: number | null }) => setCardBack(card.id, args.mode, args.id),
    onSuccess: () => setError(null),
    onError: (err: Error) => setError(err.message),
  });
  // Closes only once the choice is stored; a failure keeps the picker
  // open with onError's message, rather than closing on a choice that
  // didn't take.
  function pickAndClose(mode: BackMode, id: number | null) {
    pickMutation
      .mutateAsync({ mode, id })
      .then(onClose)
      .catch(() => {});
  }

  // Same path as the Backs tab's add: the file joins the app-global
  // library (so it can be reused, renamed, given a bleed declaration
  // there), and this card is pointed at it. It does NOT become the
  // project's universal back — that is the Backs tab's gesture.
  const uploadMutation = useMutation({
    mutationFn: async (file: File) => {
      const picked = await readPickedImage(file);
      const added = await projectApi.addBackImage({
        ...picked,
        originalFilename: file.name,
        label: file.name,
      });
      await queryClient.invalidateQueries({ queryKey: ["back-images"] });
      await setCardBack(card.id, "image", added.id);
      return added;
    },
    onSuccess: () => setError(null),
    onError: (err: Error) => setError(err.message),
  });

  const busy = pickMutation.isPending || uploadMutation.isPending;
  const quantity = card.quantity ?? 1;
  const ownCount = cards.filter((c) => c.back_mode !== "inherit").length;

  async function view(back: BackImage) {
    const src = await projectApi.backImageFull(back.id);
    if (src) setViewTarget({ title: back.label, src });
  }

  return (
    <ModalOverlay onClick={busy ? undefined : onClose}>
      <div
        className="modal card-back-modal"
        role="dialog"
        aria-label={`Back for ${card.name}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head card-back-head">
          <div style={{ minWidth: 0 }}>
            <div className="modal-title">Back for {card.printed_name ?? card.name}</div>
            <div className="hint card-back-subtitle">
              Applies to {quantity === 1 ? "the one copy" : `all ${quantity} copies`} of this card.
              Other cards keep the universal back{universal ? ` (${universal.label})` : ""}.
            </div>
          </div>
          <button type="button" className="btn-sm" onClick={onClose} disabled={busy}>
            Close
          </button>
        </div>

        {transformOnBack && (
          <div className="card-back-banner">
            <FlipIcon />
            <span>
              This card's transform side prints on its back, so the choice below is saved but
              not used. Turn off <strong>print a double-faced card's transform side on its own
              back</strong> on the PDF tab to apply it.
            </span>
          </div>
        )}

        <div className="modal-body card-back-tiles" data-tour="card-back-tiles">
          <CardBackTile
            selected={card.back_mode === "inherit"}
            onSelect={() => pickMutation.mutate({ mode: "inherit", id: null })}
            onPickAndClose={() => pickAndClose("inherit", null)}
            art={
              <>
                {universal && <LibraryTileArt back={universal} />}
                <span className="card-back-tile-glyph">
                  <LinkIcon />
                </span>
              </>
            }
            label="Universal"
            sub={universal ? `${universal.label} · Backs tab` : "None selected yet · Backs tab"}
            tour="card-back-universal"
          />
          <CardBackTile
            selected={card.back_mode === "blank"}
            onSelect={() => pickMutation.mutate({ mode: "blank", id: null })}
            onPickAndClose={() => pickAndClose("blank", null)}
            art={
              <span className="card-back-tile-glyph">
                <BlankIcon />
              </span>
            }
            label="Blank"
            sub="Nothing printed on the back"
          />
          {library.map((back) => (
            <CardBackTile
              key={back.id}
              selected={card.back_mode === "image" && card.back_image_id === back.id}
              onSelect={() => pickMutation.mutate({ mode: "image", id: back.id })}
              onPickAndClose={() => pickAndClose("image", back.id)}
              onView={() => void view(back)}
              art={<LibraryTileArt back={back} />}
              label={back.label}
              sub={`${back.width}×${back.height}${back.includes_bleed ? " · bleed included" : ""}`}
              badge={universal?.id === back.id ? "universal" : undefined}
            />
          ))}
          <div
            role="button"
            tabIndex={0}
            className="card-back-tile card-back-tile-upload"
            onClick={() => fileInput.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                fileInput.current?.click();
              }
            }}
          >
            <div className="card-back-tile-art dropzone card-back-dropzone">
              <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M20.4 14.5A4 4 0 0 0 18 7.5h-1.3A6 6 0 1 0 6 12.9" />
                <path d="M12 12v9" />
                <path d="m8.5 15.5 3.5-3.5 3.5 3.5" />
              </svg>
            </div>
            <div className="card-back-tile-label">{uploadMutation.isPending ? "Adding…" : "Upload new back"}</div>
            <div className="card-back-tile-sub">PNG, JPEG or WebP · up to {MAX_UPLOAD_MB} MB · joins the library</div>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPTED_IMAGE_TYPES}
              style={{ display: "none" }}
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file) uploadMutation.mutate(file);
              }}
            />
          </div>
        </div>

        {error && <p className="error-text" style={{ marginTop: 8 }}>{error}</p>}

        <div className="card-back-foot">
          <span className="hint">
            {ownCount === 0
              ? "Every card takes the universal back."
              : `${ownCount} of ${cards.length} cards use their own back setting.`}{" "}
            Double-click a back to pick it and close.
          </span>
          <Link to="/backs" className="linklike" onClick={onClose}>
            Manage the library on the Backs tab ↗
          </Link>
        </div>
      </div>
      {viewTarget && (
        <FullImageDialog title={viewTarget.title} src={viewTarget.src} onClose={() => setViewTarget(null)} />
      )}
    </ModalOverlay>
  );
}
