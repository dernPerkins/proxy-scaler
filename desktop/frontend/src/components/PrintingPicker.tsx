// The change-printing control: a card row's set/collector rendered as a
// button that opens an inline panel listing every printing of the card
// (variants share an oracle_id — served by GET /api/cards/variants from the
// server's locally-imported Scryfall corpus; no live Scryfall call, and a
// deliberate "import the card database first" hint when there is no corpus).
// Picking a row pins the card to that exact printing via
// ProjectContext.setCardPrinting.
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi, ApiError } from "../api/generation";
import { projectApi } from "../api/project";
import type { CardRow } from "../api/project";
import type { CardVariant } from "../api/types";

function variantYear(v: CardVariant): string {
  return v.released_at ? ` (${v.released_at.slice(0, 4)})` : "";
}

export default function PrintingPicker(props: {
  card: CardRow;
  /** The project's import-language preference — the language filter's
   *  default when the card itself doesn't carry one. */
  preferredLang: string;
  /** True when the generation server is unreachable — the button still
   *  renders the printing, it just can't open the picker. */
  disabled: boolean;
  onPick: (printing: {
    scryfallId: string;
    name: string;
    setCode: string;
    collectorNumber: string;
    lang: string;
    printedName: string | null;
  }) => void;
}) {
  const { card, preferredLang, disabled, onPick } = props;
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const cardLang = card.lang ?? preferredLang ?? "en";
  const [langFilter, setLangFilter] = useState(cardLang);

  // "Show digital" is an app-wide preference persisted in the client's
  // app_settings store — every picker instance shares this query entry,
  // so checking it on one card holds for the next and across restarts.
  const showDigitalQuery = useQuery({
    queryKey: ["show-digital"],
    queryFn: () => projectApi.getShowDigitalPrintings(),
    staleTime: Infinity,
  });
  const includeDigital = showDigitalQuery.data ?? false;
  const setShowDigital = useMutation({
    mutationFn: (show: boolean) => projectApi.setShowDigitalPrintings(show),
    onSuccess: (_data, show) => queryClient.setQueryData(["show-digital"], show),
  });

  // Lazy on purpose: nothing is fetched until the picker is opened, so a
  // 200-card decklist doesn't fire 200 variant queries on render.
  const variantsQuery = useQuery({
    queryKey: [
      "card-variants",
      card.scryfall_id ?? `${card.set_code}/${card.collector_number}/${card.name}`,
      includeDigital,
    ],
    queryFn: () =>
      generationApi.cardVariants({
        scryfall_id: card.scryfall_id,
        set_code: card.set_code,
        collector_number: card.collector_number,
        name: card.name,
        include_digital: includeDigital,
      }),
    enabled: open,
    staleTime: 60_000,
  });

  const variants = variantsQuery.data?.variants ?? [];
  const languages = useMemo(
    () => Array.from(new Set(variants.map((v) => v.lang))),
    [variants],
  );
  // The preferred filter language may simply not exist among this card's
  // printings — fall back to showing everything rather than an empty list.
  const effectiveLang = languages.includes(langFilter) ? langFilter : null;
  const shown = effectiveLang ? variants.filter((v) => v.lang === effectiveLang) : variants;

  const currentId = card.scryfall_id;
  const isCurrent = (v: CardVariant) =>
    currentId
      ? v.scryfall_id === currentId
      : v.set_code === (card.set_code ?? "").toLowerCase() &&
        v.collector_number === (card.collector_number ?? "") &&
        v.lang === cardLang;

  const label = card.set_code
    ? `${card.set_code.toUpperCase()} · ${card.collector_number ?? "—"}${
        card.lang && card.lang !== "en" ? ` · ${card.lang.toUpperCase()}` : ""
      }`
    : "— · —";

  return (
    <span className="printing-cell">
      <button
        type="button"
        className="card-meta mono printing-toggle"
        title={disabled ? "Generation server is unreachable" : "Change printing"}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        {label} ▾
      </button>
      {open && (
        <div className="printing-picker panel">
          <div className="printing-picker-controls">
            <select
              value={effectiveLang ?? "all"}
              onChange={(e) => setLangFilter(e.target.value)}
              disabled={languages.length <= 1}
            >
              {effectiveLang == null && <option value="all">All languages</option>}
              {languages.map((lang) => (
                <option key={lang} value={lang}>
                  {lang.toUpperCase()}
                </option>
              ))}
            </select>
            <label className="check">
              <input
                type="checkbox"
                checked={includeDigital}
                onChange={(e) => setShowDigital.mutate(e.target.checked)}
              />
              Show digital
            </label>
            <button type="button" className="btn-sm" onClick={() => setOpen(false)}>
              Close
            </button>
          </div>

          {variantsQuery.isLoading && <p className="hint">Loading printings…</p>}
          {variantsQuery.isError && (
            <p className="hint">
              {variantsQuery.error instanceof ApiError && variantsQuery.error.status === 404
                ? "No local card database on this server — import it from the sidebar's Card database panel to change printings."
                : `Couldn't load printings: ${
                    variantsQuery.error instanceof Error
                      ? variantsQuery.error.message
                      : String(variantsQuery.error)
                  }`}
            </p>
          )}
          {variantsQuery.isSuccess && shown.length === 0 && (
            <p className="hint">No printings match this filter.</p>
          )}

          {shown.length > 0 && (
            <div className="printing-picker-list">
              {shown.map((v) => (
                <button
                  key={v.scryfall_id}
                  type="button"
                  className={`printing-option${isCurrent(v) ? " active" : ""}`}
                  onClick={() => {
                    setOpen(false);
                    if (!isCurrent(v)) {
                      onPick({
                        scryfallId: v.scryfall_id,
                        name: v.name,
                        setCode: v.set_code,
                        collectorNumber: v.collector_number,
                        lang: v.lang,
                        printedName: v.printed_name,
                      });
                    }
                  }}
                >
                  <span className="mono">
                    {v.set_code.toUpperCase()} · #{v.collector_number}
                    {v.lang !== "en" ? ` · ${v.lang.toUpperCase()}` : ""}
                  </span>
                  <span className="printing-set-name">
                    {v.printed_name ? `${v.printed_name} · ` : ""}
                    {v.set_name ?? ""}
                    {variantYear(v)}
                    {v.digital ? " · digital" : ""}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </span>
  );
}
