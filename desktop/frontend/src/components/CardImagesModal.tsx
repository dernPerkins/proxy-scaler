// Every version of one card, in a modal over the Grid view: the Original plus
// each upscale, each carrying the Row view's own per-image controls on the
// Row view's own markup (.thumb / .thumb-label / .thumb-buttons) — an Original
// gets Download / View / Re-Fetch, an upscale gets Download / View / Compare
// (with the pick-any-other caret) / Regen, plus the row's "· queued" and
// "· CPU" notes.
//
// One deliberate addition to that set: the Row has no View on an upscale,
// because its thumb is already on screen at arm's length. In a modal of them,
// opening one full-size is the reason to be here.
//
// The grid never reflows to show a card's versions — this is the Grid view's
// Show images (DeckGrid.tsx), opened from the eye on the art or from the
// right-click menu.
import { useEffect, useState } from "react";
import { generationApi } from "../api/generation";
import type { CardRow } from "../api/project";
import type { CustomImage } from "../api/types";
import { modelDisplayName } from "../constants";
import { runDownload, slugForFilename, useDownloadStatus } from "../download";
import { fillFrameWith, trimGeometry } from "../cardTrim";
import { errorText, toast } from "../toasts";
import type { VariantStatus } from "../mergeCardStatus";
import CompareDialog from "./CompareDialog";
import CompareMenu, { type CompareSide } from "./CompareMenu";
import FullImageDialog from "./FullImageDialog";
import ModalOverlay from "./ModalOverlay";
import {
  SOURCE_MODELS,
  faceLabelTitle,
  faceOriginalDpi,
  type DisplayFace,
} from "../cardCoverage";

export default function CardImagesModal(props: {
  card: CardRow;
  /** The same merged faces the Row view draws. */
  faces: DisplayFace[];
  /** The Custom Image library, for the bleed a face's art carries. */
  library: ReadonlyMap<number, CustomImage>;
  serverUnavailable: boolean;
  /** Only worth saying on a machine that has a GPU and fell back off it. */
  warnCpu: boolean;
  onRefetch: (galleryItemId: number) => void;
  onRegen: (galleryItemId: number) => void;
  onClose: () => void;
}) {
  const { card, faces, library, serverUnavailable, warnCpu, onRefetch, onRegen, onClose } = props;
  const downloadStatus = useDownloadStatus();
  const [viewTarget, setViewTarget] = useState<{
    title: string;
    src: string;
    setCode?: string | null;
    bleedMm: number;
  } | null>(null);
  const [compare, setCompare] = useState<{
    mine: CompareSide;
    against: CompareSide;
    options: CompareSide[];
  } | null>(null);

  // Escape closes the topmost thing open. FullImageDialog handles its own.
  useEffect(() => {
    if (viewTarget) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (compare) setCompare(null);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [compare, viewTarget, onClose]);

  const saveFile = async (url: string, filename: string) => {
    try {
      await runDownload(filename, { url });
    } catch (err) {
      toast(`Download failed: ${errorText(err)}`, "err");
    }
  };

  return (
    <ModalOverlay onClick={onClose}>
      <div className="modal card-images" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">
            {card.name} — every version
            {serverUnavailable && (
              <span className="card-images-note mono">Generation Server Unreachable</span>
            )}
          </span>
          <button className="btn-sm" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="modal-body card-images-body">
          {faces.map((face, i) => {
            const done = face.variants.filter(
              (v): v is VariantStatus & { galleryItemId: number } => v.galleryItemId != null,
            );
            // Same split the row makes: the source-tier variant IS the
            // original, so it gets no Compare / Regen (it would compare
            // against itself), and the upscales below are the comparable ones.
            const source = done.find((v) => SOURCE_MODELS.includes(v.model)) ?? done[0];
            const ups = done.filter((v) => !SOURCE_MODELS.includes(v.model));
            const artImage = library.get(
              (face.art ? face.art.custom_image_id : card.custom_image_id) ?? -1,
            );
            const bleedMm = artImage?.includes_bleed ? artImage.bleed_mm : 0;
            const faceName = `${card.name}${face.faceLabel ? ` (${faceLabelTitle(face.faceLabel)})` : ""}`;
            const faceDpi = faceOriginalDpi(face, card, library);
            const sides: CompareSide[] = [
              ...(source
                ? [
                    {
                      key: "original",
                      label: "Original",
                      url: generationApi.imageUrl(source.galleryItemId, "original"),
                    },
                  ]
                : []),
              ...ups.map((v) => ({
                key: `${v.dpi}:${v.model}`,
                label: `${v.dpi} DPI · ${modelDisplayName(v.model)}`,
                url: generationApi.imageUrl(v.galleryItemId, "full"),
              })),
            ];
            const openCompare = (mineKey: string, againstKey?: string) => {
              const mine = sides.find((s) => s.key === mineKey);
              if (!mine) return;
              const options = sides.filter((s) => s.key !== mineKey);
              const against = options.find((s) => s.key === againstKey) ?? options[0];
              if (!against) return;
              setCompare({ mine, against, options });
            };
            return (
              <section key={i} className="card-images-face">
                {face.faceLabel && (
                  <div className="card-images-face-label">{faceLabelTitle(face.faceLabel)}</div>
                )}
                <div className="thumbs">
                  {source && (
                    <div className="thumb">
                      <div className="thumb-label">
                        Original{faceDpi != null && <span className="mono"> · {faceDpi} DPI</span>}
                        {source.status !== "done" && (
                          <span className="thumb-regen-note">
                            {" "}
                            · {source.status === "running" ? "downloading…" : "queued"}
                          </span>
                        )}
                      </div>
                      <ThumbImage
                        src={generationApi.imageUrl(source.galleryItemId, "original")}
                        alt={card.name}
                        bleedMm={bleedMm}
                      />
                      <div className="thumb-buttons">
                        <button
                          className="btn-sm"
                          disabled={downloadStatus != null}
                          onClick={() =>
                            void saveFile(
                              generationApi.imageUrl(source.galleryItemId, "original"),
                              `${slugForFilename(card.name)}-original.png`,
                            )
                          }
                        >
                          {downloadStatus ? "Downloading…" : "Download"}
                        </button>
                        <button
                          className="btn-sm"
                          onClick={() =>
                            setViewTarget({
                              title: `${faceName} — Original`,
                              src: generationApi.imageUrl(source.galleryItemId, "original"),
                              setCode: face.art?.scryfall?.set_code ?? card.set_code,
                              bleedMm,
                            })
                          }
                        >
                          View
                        </button>
                        {/* Nothing to re-fetch from for a Custom Image or MPC
                            Fill art: its source is the file the user owns. */}
                        {face.art?.scryfall || (!face.art && card.custom_image_id == null) ? (
                          <button
                            className="btn-sm"
                            onClick={() => onRefetch(source.galleryItemId)}
                            disabled={serverUnavailable}
                            title="Re-download the original from Scryfall (overwrites the cached copy)"
                          >
                            Re-Fetch
                          </button>
                        ) : null}
                      </div>
                    </div>
                  )}
                  {ups.map((v) => (
                    <div key={`${v.dpi}-${v.model}`} className="thumb">
                      <div className="thumb-label" title={`${v.dpi} DPI · ${v.model}`}>
                        {v.dpi} DPI · {modelDisplayName(v.model)}
                        {v.status !== "done" && (
                          <span className="thumb-regen-note">
                            {" "}
                            · {v.status === "running" ? "regenerating…" : "queued"}
                          </span>
                        )}
                        {warnCpu && v.device === "cpu" && (
                          <span
                            className="thumb-cpu-note"
                            title="Generated on the CPU — the GPU ran out of memory during this generation. Regen to retry on the GPU."
                          >
                            {" "}
                            · CPU
                          </span>
                        )}
                      </div>
                      <ThumbImage
                        src={generationApi.imageUrl(v.galleryItemId, "thumb")}
                        alt={card.name}
                        bleedMm={bleedMm}
                      />
                      <div className="thumb-buttons">
                        <button
                          className="btn-sm"
                          disabled={downloadStatus != null}
                          onClick={() =>
                            void saveFile(
                              generationApi.imageUrl(v.galleryItemId, "full"),
                              `${slugForFilename(card.name)}-${v.dpi}dpi-${v.model}.png`,
                            )
                          }
                        >
                          {downloadStatus ? "Downloading…" : "Download"}
                        </button>
                        <button
                          className="btn-sm"
                          onClick={() =>
                            setViewTarget({
                              title: `${faceName} — ${v.dpi} DPI · ${modelDisplayName(v.model)}`,
                              src: generationApi.imageUrl(v.galleryItemId, "full"),
                              setCode: face.art?.scryfall?.set_code ?? card.set_code,
                              bleedMm,
                            })
                          }
                        >
                          View
                        </button>
                        <span className="compare-split">
                          <button className="btn-sm" onClick={() => openCompare(`${v.dpi}:${v.model}`)}>
                            Compare
                          </button>
                          {sides.length > 2 && (
                            <CompareMenu
                              options={sides.filter((s) => s.key !== `${v.dpi}:${v.model}`)}
                              onPick={(side) => openCompare(`${v.dpi}:${v.model}`, side.key)}
                            />
                          )}
                        </span>
                        <button
                          className="btn-sm"
                          onClick={() => onRegen(v.galleryItemId)}
                          disabled={serverUnavailable || v.status !== "done"}
                          title={
                            v.status !== "done"
                              ? "A regeneration for this variant is already queued"
                              : undefined
                          }
                        >
                          Regen
                        </button>
                      </div>
                    </div>
                  ))}
                  {!source && <span className="variant-note">Nothing generated for this face yet.</span>}
                </div>
              </section>
            );
          })}
          {faces.length === 0 && <p className="empty-note">Nothing generated for this card yet.</p>}
        </div>
      </div>
      {compare && (
        <CompareDialog
          cardName={card.name}
          mine={compare.mine}
          against={compare.against}
          options={compare.options}
          onChangeAgainst={(side) => setCompare((t) => (t ? { ...t, against: side } : t))}
          onClose={() => setCompare(null)}
        />
      )}
      {viewTarget && (
        <FullImageDialog
          title={viewTarget.title}
          src={viewTarget.src}
          setCode={viewTarget.setCode}
          bleedMm={viewTarget.bleedMm}
          onClose={() => setViewTarget(null)}
        />
      )}
    </ModalOverlay>
  );
}

/** The row's bleed-aware thumbnail (DecklistPage.tsx::ThumbImage), same frame. */
function ThumbImage({ src, alt, bleedMm }: { src: string; alt: string; bleedMm: number }) {
  if (bleedMm <= 0) return <img src={src} alt={alt} loading="lazy" />;
  return (
    <div className="thumb-frame">
      <img src={src} alt={alt} loading="lazy" style={fillFrameWith(trimGeometry(null, bleedMm).trim)} />
    </div>
  );
}
