// The MPC Fill tab's search and filter row: one toolbar above the library
// grid, laid out like the Decklist's MPC Fill picker, so a library of
// hundreds of downloads can be narrowed to the art wanted. Chosen from a
// prototype (.scratch/mpcfill-art-picker/prototype-mpc-library/).
//
// "+ Search MPC Fill" at the end of the row opens the picker with the same
// search and filters (see toPickerInitial), so "not in my library" is one
// click from "find it on MPC Fill".
//
// Library rows know an image's name, uploader, DPI and date, but not its
// card type, language or tags; useMpcLibraryItems fills those in from
// /api/mpcfill/cards (the downloaded catalog), batched.
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import type { CustomImage, MpcCard, MpcCardType } from "../api/types";
import { MPC_LANGUAGES, type MpcPickerInitial } from "./MpcArtPicker";
import SelectMenu from "./SelectMenu";
import { useMpcCatalogReady } from "../mpcCatalog";

export type ProjectFilter = "all" | "in" | "out";
export type LibrarySort = "newest" | "name" | "dpi";

export interface MpcLibraryFilters {
  q: string;
  type: "ALL" | MpcCardType;
  /** MPC Fill language code, or "" for any. */
  lang: string;
  minDpi: number;
  /** Uploader, or "" for any. */
  source: string;
  project: ProjectFilter;
  nsfw: boolean;
  sort: LibrarySort;
}

export const NO_FILTERS: MpcLibraryFilters = {
  q: "",
  type: "ALL",
  lang: "",
  minDpi: 0,
  source: "",
  project: "all",
  nsfw: false,
  sort: "newest",
};

export interface MpcLibraryItem {
  image: CustomImage;
  name: string;
  source: string;
  dpi: number;
  /** "CARD" | "TOKEN" | …, or null until known. */
  type: string | null;
  lang: string | null;
  nsfw: boolean;
  inProject: boolean;
}

/** The library's MPC rows with their MPC Fill details filled in. */
export function useMpcLibraryItems(
  images: CustomImage[],
  inProject: ReadonlySet<number>,
): MpcLibraryItem[] {
  const ids = useMemo(
    () => [...new Set(images.flatMap((i) => (i.mpc ? [i.mpc.drive_id] : [])))].sort(),
    [images],
  );
  const catalogReady = useMpcCatalogReady().ready;
  const details = useQuery({
    queryKey: ["mpc-library-details", ids.join(",")],
    queryFn: async () => {
      const out = new Map<string, MpcCard>();
      for (let i = 0; i < ids.length; i += 1000) {
        const { cards } = await generationApi.mpcCards(ids.slice(i, i + 1000));
        for (const card of cards) out.set(card.drive_id, card);
      }
      return out;
    },
    // Details come from the downloaded catalog alone; without one the
    // type, language and NSFW filters have nothing to go on.
    enabled: ids.length > 0 && catalogReady,
    staleTime: Infinity,
    retry: false,
  });
  return useMemo(
    () =>
      images.map((image) => {
        const card = image.mpc ? details.data?.get(image.mpc.drive_id) : undefined;
        return {
          image,
          name: image.mpc?.name || image.label,
          source: image.mpc?.source || card?.source_name || "Unknown uploader",
          dpi: Math.round(image.source_dpi),
          type: card?.card_type ?? null,
          lang: card?.language ?? null,
          nsfw: card?.tags.includes("NSFW") ?? false,
          inProject: inProject.has(image.id),
        };
      }),
    [images, details.data, inProject],
  );
}

export function applyMpcLibraryFilters(
  items: MpcLibraryItem[],
  f: MpcLibraryFilters,
): MpcLibraryItem[] {
  const q = f.q.trim().toLowerCase();
  const out = items.filter(
    (it) =>
      (!q || it.name.toLowerCase().includes(q) || it.image.label.toLowerCase().includes(q)) &&
      (f.type === "ALL" || it.type === f.type) &&
      (!f.lang || it.lang === f.lang) &&
      (!f.source || it.source === f.source) &&
      it.dpi >= f.minDpi &&
      (f.project === "all" || (f.project === "in") === it.inProject) &&
      (f.nsfw || !it.nsfw),
  );
  const byName = (a: MpcLibraryItem, b: MpcLibraryItem) => a.name.localeCompare(b.name);
  if (f.sort === "name") out.sort(byName);
  else if (f.sort === "dpi") out.sort((a, b) => b.dpi - a.dpi || byName(a, b));
  else out.sort((a, b) => b.image.created_at.localeCompare(a.image.created_at));
  return out;
}

export function filtersActive(f: MpcLibraryFilters): boolean {
  return (
    f.q.trim() !== "" ||
    f.type !== "ALL" ||
    f.lang !== "" ||
    f.minDpi > 0 ||
    f.source !== "" ||
    f.project !== "all" ||
    f.nsfw
  );
}

/** What "+ Search MPC Fill" opens the picker with: the same name, type,
 *  language (Any included, over the picker's remembered one), DPI and
 *  NSFW choice, with Fuzzy on. MPC Fill searches every uploader, so the Uploader filter
 *  has no counterpart there. */
export function toPickerInitial(f: MpcLibraryFilters): MpcPickerInitial {
  return {
    query: f.q.trim() || undefined,
    type: f.type === "ALL" ? undefined : f.type,
    langChoice: f.lang || "any",
    minDpi: f.minDpi || undefined,
    nsfw: f.nsfw || undefined,
    // Names here are typed by hand, often partly: match loosely. (The
    // Decklist's picker searches a card's exact name, so it starts off.)
    fuzzy: true,
  };
}

function counted(items: MpcLibraryItem[], key: (it: MpcLibraryItem) => string | null) {
  const m = new Map<string, number>();
  for (const it of items) {
    const k = key(it);
    if (k != null) m.set(k, (m.get(k) ?? 0) + 1);
  }
  return [...m.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <span className="segmented" role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={o.value === value ? "active" : undefined}
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </span>
  );
}

const MIN_DPI = [0, 300, 600, 800, 1200];

export default function MpcLibraryToolbar({
  items,
  filters: f,
  onChange,
  shown,
  onSearchMpc,
  searchDisabledReason,
}: {
  items: MpcLibraryItem[];
  filters: MpcLibraryFilters;
  onChange: (next: MpcLibraryFilters) => void;
  /** How many tiles the filters leave. */
  shown: number;
  onSearchMpc: () => void;
  searchDisabledReason: string | null;
}) {
  const set = (patch: Partial<MpcLibraryFilters>) => onChange({ ...f, ...patch });
  const langName = (code: string) => MPC_LANGUAGES.find((l) => l.code === code)?.name ?? code;
  return (
    <div className="mpc-library-toolbar" data-tour="mpc-library-filters">
      <input
        type="search"
        placeholder="Search your MPC Fill art"
        aria-label="Search downloaded MPC Fill art"
        value={f.q}
        onChange={(e) => set({ q: e.target.value })}
      />
      <Segmented
        label="Card type"
        options={[
          { value: "ALL", label: "All" },
          { value: "CARD", label: "Cards" },
          { value: "TOKEN", label: "Tokens" },
        ]}
        value={f.type === "CARDBACK" ? "ALL" : f.type}
        onChange={(type) => set({ type })}
      />
      <SelectMenu
        ariaLabel="Language"
        options={[
          { value: "", label: "Any language" },
          ...counted(items, (it) => it.lang).map(([code, n]) => ({
            value: code,
            label: `${langName(code)} (${n})`,
          })),
        ]}
        value={f.lang}
        onChange={(lang) => set({ lang })}
      />
      <SelectMenu
        ariaLabel="Minimum DPI"
        options={MIN_DPI.map((d) => ({ value: String(d), label: d ? `${d}+ DPI` : "Any DPI" }))}
        value={String(f.minDpi)}
        onChange={(v) => set({ minDpi: Number(v) })}
      />
      <SelectMenu
        ariaLabel="Uploader"
        searchable
        searchPlaceholder="Search uploaders"
        options={[
          { value: "", label: "Any uploader" },
          ...counted(items, (it) => it.source).map(([source, n]) => ({
            value: source,
            label: `${source} (${n})`,
          })),
        ]}
        value={f.source}
        onChange={(source) => set({ source })}
      />
      <SelectMenu
        ariaLabel="Sort"
        options={[
          { value: "newest", label: "Newest First" },
          { value: "name", label: "Name" },
          { value: "dpi", label: "Highest DPI" },
        ]}
        value={f.sort}
        onChange={(v) => set({ sort: v as LibrarySort })}
      />
      <Segmented
        label="In project"
        options={[
          { value: "all", label: "All" },
          { value: "in", label: "In Project" },
          { value: "out", label: "Not in Project" },
        ]}
        value={f.project}
        onChange={(project) => set({ project })}
      />
      <label className="check" title="Include art MPC Fill tags as NSFW">
        <input type="checkbox" checked={f.nsfw} onChange={(e) => set({ nsfw: e.target.checked })} />
        NSFW
      </label>
      <span className="mpc-library-count">
        {shown} of {items.length}
      </span>
      <span className="mpc-library-spacer" />
      <button
        type="button"
        className="btn-sm btn-primary"
        data-tour="mpc-search"
        disabled={searchDisabledReason != null}
        title={
          searchDisabledReason ??
          (filtersActive(f)
            ? "Search MPC Fill with these filters"
            : "Search community art on MPC Fill")
        }
        onClick={onSearchMpc}
      >
        + Search MPC Fill
      </button>
    </div>
  );
}
