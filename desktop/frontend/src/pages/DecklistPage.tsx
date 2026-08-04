import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import CompareDialog from "../components/CompareDialog";
import ServerSwitcher from "../components/ServerSwitcher";
import StatusBadge from "../components/StatusBadge";
import { useServerReadiness } from "../config";
import { useProject } from "../context/ProjectContext";
import { downloadBlob } from "../download";
import type { Card, Variant } from "../api/types";

const DPI_OPTIONS = [600, 800, 1200];

export default function DecklistPage() {
  const queryClient = useQueryClient();
  const { projectId, settings, setSettings } = useProject();
  const readiness = useServerReadiness();

  // Always read this from the API, never hardcode — see api/client.ts's
  // listModels comment for the regression this replaced.
  const modelsQuery = useQuery({ queryKey: ["models"], queryFn: () => api.listModels() });

  const [decklistText, setDecklistText] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [sortPrimary, setSortPrimary] = useState<"Name" | "Set" | "(none)">("Name");
  const [expandedFaces, setExpandedFaces] = useState<Set<string>>(new Set());

  const cardsQuery = useQuery({
    queryKey: ["cards", projectId],
    queryFn: () => api.listCards(projectId as number),
    enabled: projectId != null,
    refetchInterval: 3000,
  });

  const importMutation = useMutation({
    mutationFn: (text: string) => api.importDecklist(projectId as number, text),
    onSuccess: (result) => {
      setStatus(
        `Imported ${result.added} new card(s)` +
          (result.skipped ? `, ${result.skipped} already in the list` : "") +
          (result.failed ? `, ${result.failed} failed to resolve` : "") +
          ".",
      );
      queryClient.invalidateQueries({ queryKey: ["cards", projectId] });
    },
  });

  const removeCardMutation = useMutation({
    mutationFn: (cardId: number) => api.removeCard(projectId as number, cardId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["cards", projectId] }),
  });

  const generateAllMutation = useMutation({
    mutationFn: () =>
      api.generate({
        project_id: projectId as number,
        model: settings.model,
        dpi_targets: settings.dpi_targets,
        skip_existing: settings.skip_existing,
        tile_size: settings.tile_size,
        output_dir: settings.output_dir,
        cache_dir: settings.cache_dir,
        weights_dir: settings.weights_dir,
      }),
    onSuccess: (result) => {
      setStatus(
        result.queued
          ? `Queued ${result.queued} task(s) — see the Tasks tab to monitor progress.`
          : "Nothing to do — every requested image already exists.",
      );
      queryClient.invalidateQueries({ queryKey: ["cards", projectId] });
    },
  });

  const generateCardMutation = useMutation({
    mutationFn: (card: Card) =>
      api.generate({
        project_id: projectId as number,
        card_ids: [card.id],
        model: settings.model,
        dpi_targets: settings.dpi_targets,
        skip_existing: settings.skip_existing,
        tile_size: settings.tile_size,
        output_dir: settings.output_dir,
        cache_dir: settings.cache_dir,
        weights_dir: settings.weights_dir,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["cards", projectId] }),
  });

  const regenerateMutation = useMutation({
    mutationFn: (galleryItemId: number) =>
      api.regenerateGalleryItem(projectId as number, galleryItemId, {
        tile_size: settings.tile_size,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["cards", projectId] }),
  });

  const [confirmClearGenerated, setConfirmClearGenerated] = useState(false);
  const clearGeneratedMutation = useMutation({
    mutationFn: () => api.clearGeneratedData(settings.output_dir, settings.cache_dir),
    onSuccess: () => {
      setConfirmClearGenerated(false);
      queryClient.invalidateQueries({ queryKey: ["cards"] });
    },
  });

  function toggleDpi(dpi: number) {
    setSettings((s) => ({
      ...s,
      dpi_targets: s.dpi_targets.includes(dpi)
        ? s.dpi_targets.filter((d) => d !== dpi)
        : [...s.dpi_targets, dpi].sort((a, b) => a - b),
    }));
  }

  function toggleExpanded(key: string) {
    setExpandedFaces((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const cards: Card[] = cardsQuery.data ?? [];
  const sortedCards = sortCards(cards, sortPrimary);

  return (
    <div className="layout">
      <aside className="sidebar panel">
        <h3 style={{ marginBottom: 14 }}>Settings</h3>

        {/* Above the field group, and deliberately outside the
            no-project branch below: switching to an empty server leaves
            projectId null, and if this lived behind that check there'd be
            no way to switch back. */}
        <ServerSwitcher />

        <div className="field-group">
          <label className="field">
            <span>Upscale model</span>
            <select
              value={settings.model}
              onChange={(e) => setSettings((s) => ({ ...s, model: e.target.value }))}
            >
              {(modelsQuery.data ?? []).map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>

          <div className="field">
            <span>Target DPI</span>
            <div className="check-row">
              {DPI_OPTIONS.map((dpi) => (
                <label key={dpi} className="check">
                  <input
                    type="checkbox"
                    checked={settings.dpi_targets.includes(dpi)}
                    onChange={() => toggleDpi(dpi)}
                  />
                  {dpi}
                </label>
              ))}
            </div>
          </div>

          <label className="check">
            <input
              type="checkbox"
              checked={settings.skip_existing}
              onChange={(e) => setSettings((s) => ({ ...s, skip_existing: e.target.checked }))}
            />
            Skip existing output files
          </label>

          <label className="field">
            <span>Tile size (0 = auto)</span>
            <input
              type="number"
              min={0}
              step={32}
              value={settings.tile_size}
              onChange={(e) => setSettings((s) => ({ ...s, tile_size: Number(e.target.value) }))}
            />
          </label>

          <label className="field">
            <span>Output directory</span>
            <input
              value={settings.output_dir}
              onChange={(e) => setSettings((s) => ({ ...s, output_dir: e.target.value }))}
            />
          </label>

          <label className="field">
            <span>Cache directory</span>
            <input
              value={settings.cache_dir}
              onChange={(e) => setSettings((s) => ({ ...s, cache_dir: e.target.value }))}
            />
          </label>

          <label className="field">
            <span>Weights directory</span>
            <input
              value={settings.weights_dir}
              onChange={(e) => setSettings((s) => ({ ...s, weights_dir: e.target.value }))}
            />
          </label>
        </div>

        <div className="danger-zone">
          <h3>Danger zone</h3>
          <p className="hint">
            Deletes the output and cache directories above. Model weights are kept.
          </p>
          <label className="check">
            <input
              type="checkbox"
              checked={confirmClearGenerated}
              onChange={(e) => setConfirmClearGenerated(e.target.checked)}
            />
            Confirm delete generated data
          </label>
          <button
            className="btn-danger btn-block"
            onClick={() => clearGeneratedMutation.mutate()}
            disabled={!confirmClearGenerated || clearGeneratedMutation.isPending}
          >
            Delete all generated images &amp; cache
          </button>
          {clearGeneratedMutation.data && (
            <div className="hint">
              {clearGeneratedMutation.data.notes.length > 0 ? (
                clearGeneratedMutation.data.notes.map((note, i) => <div key={i}>{note}</div>)
              ) : (
                <div>Generated data cleared.</div>
              )}
            </div>
          )}
        </div>
      </aside>

      <main className="content">
        <h2>Decklist</h2>

        {projectId == null ? (
          <p className="hint" style={{ marginTop: 10 }}>
            {readiness.status === "starting"
              ? "Starting local server — your last project will load automatically."
              : "Enter a project name in the project bar above and click Save to get started."}
          </p>
        ) : (
          <>
            <div className="import-box panel" style={{ marginTop: 10 }}>
              <textarea
                value={decklistText}
                onChange={(e) => setDecklistText(e.target.value)}
                rows={6}
                style={{ width: "100%" }}
                placeholder={"1 Sol Ring (c21) 263\n4 Lightning Bolt"}
              />
              <button
                className="btn-primary"
                onClick={() => importMutation.mutate(decklistText)}
                disabled={!decklistText.trim() || importMutation.isPending}
              >
                {importMutation.isPending ? "Importing…" : "Import cards"}
              </button>
              {status && <p className="hint">{status}</p>}
            </div>

            <div className="decklist-head">
              <h2>
                Cards <span style={{ color: "var(--text-faint)" }}>({cards.length})</span>
              </h2>
              <div className="decklist-actions">
                <select
                  value={sortPrimary}
                  onChange={(e) => setSortPrimary(e.target.value as typeof sortPrimary)}
                >
                  <option value="Name">Sort: Name</option>
                  <option value="Set">Sort: Set</option>
                  <option value="(none)">Sort: (none)</option>
                </select>
                <button
                  className="btn-primary"
                  onClick={() => generateAllMutation.mutate()}
                  disabled={!cards.length || generateAllMutation.isPending}
                >
                  Generate upscaled images
                </button>
              </div>
            </div>

            {cards.length === 0 && !cardsQuery.isLoading && (
              <p className="empty-note">No cards yet — paste a decklist above and import it.</p>
            )}

            {sortedCards.map((card) => (
              <CardRow
                key={card.id}
                card={card}
                projectId={projectId}
                expandedFaces={expandedFaces}
                onToggleExpand={toggleExpanded}
                onRemove={() => removeCardMutation.mutate(card.id)}
                onGenerate={() => generateCardMutation.mutate(card)}
                onRegenerate={(galleryItemId) => regenerateMutation.mutate(galleryItemId)}
              />
            ))}
          </>
        )}
      </main>
    </div>
  );
}

function sortCards(cards: Card[], primary: "Name" | "Set" | "(none)"): Card[] {
  if (primary === "(none)") return cards;
  const key = (c: Card) =>
    (primary === "Name" ? (c.card_name ?? "") : (c.set_code ?? "")).toLowerCase();
  return [...cards].sort((a, b) => key(a).localeCompare(key(b)));
}

function slugify(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "card";
}

async function handleDownloadImage(url: string, filename: string): Promise<void> {
  const resp = await fetch(url);
  const blob = await resp.blob();
  await downloadBlob(blob, filename);
}

interface CompareTarget {
  originalUrl: string;
  upscaledUrl: string;
  label: string;
}

function CardRow(props: {
  card: Card;
  projectId: number;
  expandedFaces: Set<string>;
  onToggleExpand: (key: string) => void;
  onRemove: () => void;
  onGenerate: () => void;
  onRegenerate: (galleryItemId: number) => void;
}) {
  const { card, projectId, expandedFaces, onToggleExpand, onRemove, onGenerate, onRegenerate } =
    props;
  const rowKey = `card-${card.id}`;
  const expanded = expandedFaces.has(rowKey);
  const hasImages = card.faces.some((f) => f.variants.some((v) => v.status === "done"));
  const [compareTarget, setCompareTarget] = useState<CompareTarget | null>(null);

  return (
    <div className="card-row">
      <div className="card-main">
        <span className="card-name">{card.card_name ?? card.original_import_line}</span>
        <span className="card-meta mono">{(card.set_code ?? "—").toUpperCase()}</span>
        <span className="card-meta mono">{card.collector_number ?? "—"}</span>
        <span className="card-qty">×{card.quantity ?? 1}</span>
        <span className="card-buttons">
          {hasImages && (
            <button className="btn-sm" onClick={() => onToggleExpand(rowKey)}>
              {expanded ? "Hide" : "Show"}
            </button>
          )}
          <button className="btn-sm" onClick={onGenerate}>
            Generate
          </button>
          <button className="btn-sm btn-danger" onClick={onRemove}>
            Remove
          </button>
        </span>
      </div>

      {card.faces.length === 0 ? (
        <div className="empty-note">Not generated yet.</div>
      ) : (
        card.faces.map((face, i) => (
          <div key={i} className="variants">
            {face.face_label && <span className="variant-face">{face.face_label}</span>}
            {face.variants.map((v) => (
              <StatusBadge key={`${v.dpi}-${v.model}`} status={v.status}>
                {v.dpi} · {v.model}
              </StatusBadge>
            ))}
          </div>
        ))
      )}

      {expanded &&
        card.faces.map((face, i) => (
          <div key={i} className="thumbs">
            {face.variants
              .filter(
                (v): v is Variant & { gallery_item_id: number } =>
                  v.status === "done" && v.gallery_item_id != null,
              )
              .map((v) => (
                <div key={`${v.dpi}-${v.model}`} className="thumb">
                  <div className="thumb-label">
                    {v.dpi} DPI · {v.model}
                  </div>
                  <img
                    src={api.imageUrl(projectId, v.gallery_item_id, "full")}
                    alt={card.card_name ?? ""}
                    loading="lazy"
                  />
                  <div className="thumb-buttons">
                    <button
                      className="btn-sm"
                      onClick={() =>
                        handleDownloadImage(
                          api.imageUrl(projectId, v.gallery_item_id, "full"),
                          `${slugify(card.card_name ?? "card")}-${v.dpi}dpi-${v.model}.png`,
                        )
                      }
                    >
                      Download
                    </button>
                    <button
                      className="btn-sm"
                      onClick={() =>
                        setCompareTarget({
                          originalUrl: api.imageUrl(projectId, v.gallery_item_id, "original"),
                          upscaledUrl: api.imageUrl(projectId, v.gallery_item_id, "full"),
                          label: `${card.card_name ?? "Card"} — ${v.dpi} DPI · ${v.model}`,
                        })
                      }
                    >
                      Compare
                    </button>
                    <button className="btn-sm" onClick={() => onRegenerate(v.gallery_item_id)}>
                      Regen
                    </button>
                  </div>
                </div>
              ))}
          </div>
        ))}

      {compareTarget && (
        <CompareDialog
          originalUrl={compareTarget.originalUrl}
          upscaledUrl={compareTarget.upscaledUrl}
          label={compareTarget.label}
          onClose={() => setCompareTarget(null)}
        />
      )}
    </div>
  );
}
