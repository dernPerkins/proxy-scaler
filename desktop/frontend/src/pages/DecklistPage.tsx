import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import CompareDialog from "../components/CompareDialog";
import { useServerReadiness } from "../config";
import { useProject } from "../context/ProjectContext";
import { downloadBlob } from "../download";
import type { Card, Variant } from "../api/types";

const DPI_OPTIONS = [600, 800, 1200];

const STATUS_ICON: Record<string, string> = {
  pending: "⏳",
  running: "⚙️",
  done: "✅",
  failed: "❌",
  canceled: "🚫",
};

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

  if (projectId == null) {
    return (
      <div>
        <h2>Decklist</h2>
        {readiness.status === "starting" ? (
          <p>Starting local server — your last project will load automatically.</p>
        ) : (
          <p>Enter a project name in the project bar above and click Save to get started.</p>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", gap: 24 }}>
      <aside style={{ width: 260, flexShrink: 0 }}>
        <h3>Settings</h3>
        <label>
          Upscale model
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
        <div>
          <div>Target DPI</div>
          {DPI_OPTIONS.map((dpi) => (
            <label key={dpi} style={{ marginRight: 8 }}>
              <input
                type="checkbox"
                checked={settings.dpi_targets.includes(dpi)}
                onChange={() => toggleDpi(dpi)}
              />
              {dpi}
            </label>
          ))}
        </div>
        <label>
          <input
            type="checkbox"
            checked={settings.skip_existing}
            onChange={(e) => setSettings((s) => ({ ...s, skip_existing: e.target.checked }))}
          />
          Skip existing output files
        </label>
        <label>
          Tile size (0 = auto)
          <input
            type="number"
            min={0}
            step={32}
            value={settings.tile_size}
            onChange={(e) => setSettings((s) => ({ ...s, tile_size: Number(e.target.value) }))}
          />
        </label>
        <label>
          Output directory
          <input
            value={settings.output_dir}
            onChange={(e) => setSettings((s) => ({ ...s, output_dir: e.target.value }))}
          />
        </label>
        <label>
          Cache directory
          <input
            value={settings.cache_dir}
            onChange={(e) => setSettings((s) => ({ ...s, cache_dir: e.target.value }))}
          />
        </label>
        <label>
          Weights directory
          <input
            value={settings.weights_dir}
            onChange={(e) => setSettings((s) => ({ ...s, weights_dir: e.target.value }))}
          />
        </label>

        <hr style={{ margin: "16px 0" }} />
        <p style={{ fontSize: 12, opacity: 0.7 }}>
          Deletes the output and cache directories above (keeps model weights).
        </p>
        <label>
          <input
            type="checkbox"
            checked={confirmClearGenerated}
            onChange={(e) => setConfirmClearGenerated(e.target.checked)}
          />
          Confirm delete generated data
        </label>
        <button
          onClick={() => clearGeneratedMutation.mutate()}
          disabled={!confirmClearGenerated || clearGeneratedMutation.isPending}
        >
          Delete all generated images & cache
        </button>
        {clearGeneratedMutation.data && (
          <div style={{ fontSize: 12, marginTop: 4 }}>
            {clearGeneratedMutation.data.notes.length > 0 ? (
              clearGeneratedMutation.data.notes.map((note, i) => <div key={i}>{note}</div>)
            ) : (
              <div>Generated data cleared.</div>
            )}
          </div>
        )}
      </aside>

      <main style={{ flex: 1 }}>
        <h2>Decklist</h2>
        <textarea
          value={decklistText}
          onChange={(e) => setDecklistText(e.target.value)}
          rows={8}
          style={{ width: "100%" }}
          placeholder={"1 Sol Ring (c21) 263\n4 Lightning Bolt"}
        />
        <button
          onClick={() => importMutation.mutate(decklistText)}
          disabled={!decklistText.trim() || importMutation.isPending}
        >
          Import cards
        </button>
        {status && <p>{status}</p>}

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3>Cards ({cards.length})</h3>
          <div>
            <select
              value={sortPrimary}
              onChange={(e) => setSortPrimary(e.target.value as typeof sortPrimary)}
            >
              <option value="Name">Sort: Name</option>
              <option value="Set">Sort: Set</option>
              <option value="(none)">Sort: (none)</option>
            </select>
            <button
              onClick={() => generateAllMutation.mutate()}
              disabled={!cards.length || generateAllMutation.isPending}
            >
              Generate upscaled images
            </button>
          </div>
        </div>

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
      </main>
    </div>
  );
}

function sortCards(cards: Card[], primary: "Name" | "Set" | "(none)"): Card[] {
  if (primary === "(none)") return cards;
  const key = (c: Card) => (primary === "Name" ? (c.card_name ?? "") : (c.set_code ?? "")).toLowerCase();
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
  const { card, projectId, expandedFaces, onToggleExpand, onRemove, onGenerate, onRegenerate } = props;
  const rowKey = `card-${card.id}`;
  const expanded = expandedFaces.has(rowKey);
  const hasImages = card.faces.some((f) => f.variants.some((v) => v.status === "done"));
  const [compareTarget, setCompareTarget] = useState<CompareTarget | null>(null);

  return (
    <div style={{ borderTop: "1px solid #444", padding: "8px 0" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <strong style={{ flex: 3 }}>{card.card_name ?? card.original_import_line}</strong>
        <span style={{ flex: 1 }}>{(card.set_code ?? "—").toUpperCase()}</span>
        <span style={{ flex: 1 }}>{card.collector_number ?? "—"}</span>
        <span style={{ flex: 0.5 }}>×{card.quantity ?? 1}</span>
        {hasImages && (
          <button onClick={() => onToggleExpand(rowKey)}>{expanded ? "Hide" : "Show"}</button>
        )}
        <button onClick={onGenerate}>Generate</button>
        <button onClick={onRemove}>Remove</button>
      </div>

      {card.faces.length === 0 ? (
        <div style={{ color: "#888" }}>Not generated yet.</div>
      ) : (
        card.faces.map((face, i) => (
          <div key={i}>
            {(face.face_label ? `${face.face_label}: ` : "") +
              face.variants
                .map((v) => `${STATUS_ICON[v.status] ?? "•"} ${v.dpi}·${v.model} ${v.status}`)
                .join("   ")}
          </div>
        ))
      )}

      {expanded &&
        card.faces.map((face, i) => (
          <div key={i} style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 8 }}>
            {face.variants
              .filter((v): v is Variant & { gallery_item_id: number } => v.status === "done" && v.gallery_item_id != null)
              .map((v) => (
                <div key={`${v.dpi}-${v.model}`} style={{ width: 160 }}>
                  <div>
                    {v.dpi} DPI · {v.model}
                  </div>
                  <img
                    src={api.imageUrl(projectId, v.gallery_item_id, "full")}
                    alt={card.card_name ?? ""}
                    style={{ width: "100%" }}
                    loading="lazy"
                  />
                  <button
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
                  <button onClick={() => onRegenerate(v.gallery_item_id)}>Regen</button>
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
