// The selected library image's Upscale button and its badges — everything
// ever queued or made for it, whichever project (or the library tab) did
// it. Shared by the Customs and Backs tabs; the page owns the mutation
// and the status poll (libraryUpscale.ts) and hands the results in.

import StatusBadge from "./StatusBadge";
import { modelDisplayName } from "../constants";
import type { VariantStatus } from "../mergeCardStatus";

interface Props {
  /** The image's DPI across a card, as the server measures it. */
  sourceDpi: number;
  variants: VariantStatus[];
  /** The DPIs a click would queue (libraryUpscale.targetsFor); empty
   *  means the image already reaches every ticked target. */
  targets: number[];
  disabledReason: string | null;
  pending: boolean;
  note: string | null;
  onUpscale: () => void;
}

export default function LibraryUpscaleControls({
  sourceDpi,
  variants,
  targets,
  disabledReason,
  pending,
  note,
  onUpscale,
}: Props) {
  const reason =
    disabledReason ??
    (targets.length === 0
      ? `Already ${Math.round(sourceDpi)} DPI — tick a target above that to upscale it.`
      : null);
  return (
    <div data-tour="library-upscale" style={{ marginTop: 14 }}>
      <button
        className="btn-sm"
        onClick={onUpscale}
        disabled={pending || reason != null}
        title={reason ?? undefined}
      >
        {pending ? "Queuing…" : `Upscale to ${targets.join(", ") || "…"} DPI`}
      </button>
      {reason ? <p className="hint">{reason}</p> : null}
      {note ? <p className="hint">{note}</p> : null}
      {variants.length > 0 && (
        <div className="variants">
          {variants.map((v) => (
            <StatusBadge key={`${v.dpi}-${v.model}`} status={v.status}>
              <span title={v.error ?? undefined}>
                {v.dpi} · {modelDisplayName(v.model)}
              </span>
            </StatusBadge>
          ))}
        </div>
      )}
    </div>
  );
}
