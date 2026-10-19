import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { generationApi } from "../api/generation";
import { projectApi } from "../api/project";
import type { CardFaceArt, CardRow, CustomUpscaleMode } from "../api/project";
import type { GalleryItem, Task } from "../api/types";
import CardDbPanel from "../components/CardDbPanel";
import MpcCatalogPanel from "../components/MpcCatalogPanel";
import SelectMenu from "../components/SelectMenu";
import UpscaleSettingsFields from "../components/UpscaleSettingsFields";
import CompareDialog from "../components/CompareDialog";
import FullImageDialog from "../components/FullImageDialog";
import CompareMenu, { type CompareSide } from "../components/CompareMenu";
import SplitButton from "../components/SplitButton";
import ConfirmDialog from "../components/ConfirmDialog";
import NumberInput from "../components/NumberInput";
import PrintingPicker from "../components/PrintingPicker";
import CardBackPicker from "../components/CardBackPicker";
import {
  DownloadIcon,
  EyeIcon,
  EyeOffIcon,
  SparklesIcon,
  TrashIcon,
} from "../components/RowIcons";
import ServerSwitcher from "../components/ServerSwitcher";
import StatusBadge from "../components/StatusBadge";
import {
  CUSTOM_SOURCE_MODEL,
  DEFAULT_GEN_PATHS,
  ORIGINAL_MODEL,
  modelDisplayName,
} from "../constants";
import { useConnection } from "../connection";
import {
  getProbedDevice,
  serverSupportsCustomUpscale,
  serverSupportsMpcFill,
  serverSupportsSetIcons,
  subscribeProbedDevice,
  useServerReadiness,
  useServerVersion,
} from "../config";
import { invokeOpenDirectory, invokeOpenRemoteTerminal, isTauri } from "../tauri";
import { resolveImageOrigin } from "../imageOrigin";
import { hasShortcutModifier, isDialogOpen, shortcutLabel } from "../shortcuts";
import { useProject } from "../context/ProjectContext";
import { cardToEntry, sortCards } from "../deckEntries";
import { SCOPE_EMPTY, SCOPE_LABELS, scopeRequest, type UpscaleScope } from "../upscaleScope";
import SortSelect from "../components/SortSelect";
import { getProjectSnapshot, registerCustomCards, syncCustomImages } from "../syncCustoms";
import { alternateName } from "../cardNames";
import { importMpcOrder } from "../mpcFill";
import {
  autoPickMpcArt,
  fetchPendingFaceArt,
  hasPendingFaceArt,
  dismissPendingFaceArt,
  downloadPickedPrinting,
  pendingFaceArtKey,
  pendingPrintingKey,
  queueFaceArtPick,
  usePendingFaceArt,
  type PendingFaceArt,
} from "../pickDownloads";
import SplitPill from "../components/SplitPill";
import { parseMpcOrder } from "../mpcOrder";
import type { BackImage } from "../api/types";
import { UploadCanceled } from "../uploadProgress";
import {
  ACCEPTED_IMAGE_TYPES,
  MAX_UPLOAD_MB,
  addImagesSequentially,
} from "../imageUpload";
import { runDownload, useDownloadStatus } from "../download";
import { useTourOnFirstView, useTourWhenPresent } from "../tutorial/tutorialStore";
import {
  applyFaceArt,
  cardIdentity,
  groupByCard,
  groupByCardName,
  buildRows,
  statusForPairs,
  type CardFaceStatus,
  type VariantStatus,
} from "../mergeCardStatus";
import type { FaceArtHandlers, PickerOpenRequest } from "../components/PrintingPicker";

// Generation-machine-local directory names, fixed and not
// user-configurable: every generate/regenerate/clear request sends these
// same relative names, and the server resolves them against its own cwd.
// misc.py's /api/paths mirrors them and reports where they actually land
// (the sidebar shows that, read-only). Not in project.settings either —
// a path valid on one machine means nothing against a Remote host. See
// ARCHITECTURE.md.
// A CardRow has no scryfall_id (the client never calls Scryfall — see
// ARCHITECTURE.md), so its identity falls back to its own name when
// there's no exact set/collector, same as the gallery/task side's
// fallback in mergeCardStatus.ts. A Custom Image card is keyed on its
// content hash instead, above every other tier.
function localCardIdentity(card: CardRow): string {
  return cardIdentity(
    card.set_code, card.collector_number, null, card.name, card.lang, card.custom_hash,
  );
}

// One face of a row; `art` when it prints MPC Fill art (face_art).
type DisplayFace = CardFaceStatus<CardFaceArt> & {
  /** MPC Fill art picked for this face and still on its way. */
  pick?: PendingFaceArt;
};

/** Faces with MPC art still downloading show that instead of what they
 *  print today. Positions: 0 for a single face or a front, 1 for a back. */
function withPendingPicks(
  faces: DisplayFace[],
  cardId: number,
  pendingArt: ReadonlyMap<string, PendingFaceArt>,
): DisplayFace[] {
  // A new printing on its way replaces the whole row: none of what's
  // shown belongs to it.
  const printing = pendingArt.get(pendingPrintingKey(cardId));
  if (printing) return [{ faceLabel: null, variants: [], pick: printing }];
  const picks = [0, 1]
    .map((face) => pendingArt.get(pendingFaceArtKey(cardId, face)))
    .filter((p): p is PendingFaceArt => p != null);
  if (picks.length === 0) return faces;
  const position = (f: DisplayFace) => f.art?.face ?? (f.faceLabel === "back" ? 1 : 0);
  const picked = new Set(picks.map((p) => p.face));
  return [
    ...faces.filter((f) => !picked.has(position(f))),
    ...picks.map((pick) => ({
      faceLabel: pick.totalFaces > 1 ? (pick.face === 0 ? "front" : "back") : null,
      variants: [],
      pick,
    })),
  ].sort((a, b) => (a.pick?.face ?? position(a)) - (b.pick?.face ?? position(b)));
}

export default function DecklistPage() {
  const queryClient = useQueryClient();
  const pendingArt = usePendingFaceArt();
  const {
    projectId,
    projectTag,
    settings,
    setSettings,
    cards,
    importResolvedCards,
    addCustomCards,
    addCustomCardsWithBacks,
    removeCard,
    setCardQuantity,
    setCardPrinting,
    setCardFaceArt,
    setCardFaceArtPending,
    clearCardFaceArt,
    resetCardBacks,
    applyCardResolutions,
    backfillRarities,
  } = useProject();
  // Per-card backs: how many rows have chosen one, for the bulk reset.
  const ownBackCount = cards.filter((c) => c.back_mode !== "inherit").length;
  const [confirmResetBacks, setConfirmResetBacks] = useState(false);
  const readiness = useServerReadiness();
  const connection = useConnection();
  useTourOnFirstView("decklist");
  // The card-row controls only exist once something's been imported.
  useTourWhenPresent("decklist-cards", cards.length > 0, "decklist");
  // Whether the generation server is reachable right now — remote mode
  // has its own 30s heartbeat (connection.remoteHealthy); local mode's
  // equivalent is "has the sidecar finished starting". Generate/PDF
  // requests against an unreachable server used to just hang or fail
  // silently with no feedback; this both disables the buttons and (via
  // the mutation onError handlers below) surfaces a real error if one
  // slips through anyway.
  const serverUnavailable =
    connection.mode === "remote" ? !connection.remoteHealthy : readiness.status !== "ready";
  // Custom card images need /api/customs and hash-identified generation;
  // syncCustomImages turns this into a real error rather than letting an
  // older server silently drop the field. See config.ts.
  const serverVersion = useServerVersion();
  // What Generate actually asks for: a server older than the setting
  // would drop the field anyway, so the control is disabled and "off"
  // is sent rather than a choice it can't honour.
  const customUpscaleSupported = serverSupportsCustomUpscale(serverVersion);
  // How custom and MPC art comes out when an upscale button asks for it
  // (the buttons decide *whether*; this setting only decides the result).
  const customResult = customUpscaleSupported
    ? settings.custom_upscale === "off"
      ? "target"
      : settings.custom_upscale
    : "off";

  // Purely an entry box — never seeded from the project's stored last
  // paste (the card rows below are the actual deck; decklistText lives on
  // only as Save As's copy source in ProjectContext). Cleared when a
  // different project loads so text typed against one deck can't be
  // imported into another, and cleared again after a successful import.
  const [decklistDraft, setDecklistDraft] = useState("");
  // Cleared when switching to another project — not when the first one is
  // created under it (no project yet -> the Unnamed Project): changing any
  // setting before importing does that, and must keep what was typed.
  const draftProjectId = useRef(projectId);
  useEffect(() => {
    const previous = draftProjectId.current;
    draftProjectId.current = projectId;
    if (previous != null && previous !== projectId) setDecklistDraft("");
  }, [projectId]);
  const [status, setStatus] = useState<string | null>(null);
  // Per-line failures from the last resolve-gated import attempt — the
  // lines themselves stay in the textarea for fixing; successes leave it.
  const [importFailures, setImportFailures] = useState<
    { line: string; error: string }[]
  >([]);
  const [importing, setImporting] = useState(false);
  useEffect(() => setImportFailures([]), [projectId]);

  // The resolve-gated import: parse locally (Rust), resolve against the
  // server (strictly in the chosen language unless "All Languages"),
  // persist only the successes — failures are listed and NOT added.
  const handleImport = async () => {
    setStatus(null);
    setImportFailures([]);
    setArtNote(null);
    setImporting(true);
    try {
      const parsed = await projectApi.parseDecklist(decklistDraft);
      if (parsed.length === 0) {
        setStatus("Nothing to import.");
        return;
      }
      const lang = settings.lang_any ? null : settings.preferred_lang;
      const result = await generationApi.resolve(
        parsed.map((e) => ({
          quantity: e.quantity,
          name: e.name,
          set_code: e.set_code,
          collector_number: e.collector_number,
          raw_line: e.raw_line,
          lang,
        })),
        { strict_lang: !settings.lang_any },
      );
      const resolvedCards = result.resolved.flatMap((rc) => {
        const face = rc.faces[0];
        if (!face) return [];
        return [
          {
            raw_line: rc.raw_line,
            quantity: rc.quantity,
            name: face.card_name,
            printed_name: face.printed_name ?? null,
            flavor_name: face.flavor_name ?? null,
            set_code: face.set_code,
            collector_number: face.collector_number,
            scryfall_id: face.scryfall_id,
            lang: face.lang ?? "en",
            rarity: face.rarity ?? null,
          },
        ];
      });
      let importedCards: CardRow[] = [];
      if (resolvedCards.length > 0) {
        const before = new Set(cards.map((c) => c.id));
        const after = await importResolvedCards(decklistDraft, resolvedCards);
        importedCards = after.filter((c) => !before.has(c.id));
      }
      if (importedCards.length > 0 && settings.import_art_source === "mpc" && mpcSupported) {
        // After the import's own bookkeeping, so the cards are in the list
        // (with their Scryfall identity) while their MPC art downloads.
        void pickMpcArtFor(importedCards);
      }
      const failures = result.failed.map((f) => ({ line: f.raw_line, error: f.error }));
      if (failures.length > 0) {
        const failedLines = new Set(failures.map((f) => f.line));
        setDecklistDraft(
          decklistDraft
            .split("\n")
            .filter((l) => failedLines.has(l.trim()))
            .join("\n"),
        );
        setImportFailures(failures);
        setStatus(
          `Imported ${resolvedCards.length} card(s) — ${failures.length} line(s) didn't match and were not added.`,
        );
      } else {
        setDecklistDraft("");
        if (resolvedCards.length === 0) setStatus("Nothing to import.");
      }
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setImporting(false);
    }
  };
  const [expandedFaces, setExpandedFaces] = useState<Set<string>>(new Set());

  // Where the fixed generation directories resolve on the connected
  // server's machine — display-only. Invalidated on server switch in
  // connection.tsx, since the answer is per-server.
  const pathsQuery = useQuery({
    queryKey: ["gen-paths"],
    queryFn: () => generationApi.getPaths(),
  });

  // Local card data (decklist text -> CardRow[]) is invoke-based and only
  // changes on an explicit mutation — no polling needed, it can't go
  // stale behind the user's back. Generation status (tasks + gallery) is
  // the half that's actually live, so it keeps the old 3s poll.
  const statusQuery = useQuery({
    queryKey: ["generation-status", projectTag],
    queryFn: async () => {
      const [tasks, gallery] = await Promise.all([
        generationApi.listTasks({ project_tag: projectTag as string }),
        generationApi.listGallery(projectTag as string),
      ]);
      return { tasks, gallery };
    },
    enabled: projectTag != null,
    refetchInterval: 3000,
  });

  function invalidateStatus() {
    queryClient.invalidateQueries({ queryKey: ["generation-status", projectTag] });
  }
  /** invalidateStatus, resolving once the new status has arrived. */
  function refetchStatus() {
    return queryClient.refetchQueries({ queryKey: ["generation-status", projectTag] });
  }

  // Reconcile the gallery with the server's disk on import/load (and
  // again once the server comes up). Adoption: images generated under
  // another project exist on disk but this project has no rows, so the
  // deck would sit at "Not generated yet" until a Generate request
  // happens to skip_existing its way to them. Pruning: the reverse lie —
  // output files are shared and tag-less, so another project clearing
  // them leaves this project's rows asserting green badges for images
  // that 404. Keyed on the identity set, not `cards` itself, so quantity
  // edits don't re-fire it; best-effort because a failure only means the
  // status poll shows a stale view until the next reconcile.
  const cardIdentities = cards.map(localCardIdentity).sort().join("|");
  // Bumped when a printing picker closes: its preview may have downloaded
  // originals (registered server-side, project-less) that this deck's
  // cards should now adopt — "300 · Original" badges included — even when
  // no printing changed and so no identity did either.
  const [adoptNonce, setAdoptNonce] = useState(0);
  useEffect(() => {
    if (!projectTag || serverUnavailable || cards.length === 0) return;
    let cancelled = false;
    generationApi
      .adoptGallery(projectTag, cards.map(cardToEntry), DEFAULT_GEN_PATHS.output_dir)
      .then(({ adopted, pruned }) => {
        if (!cancelled && (adopted > 0 || pruned > 0)) {
          queryClient.invalidateQueries({ queryKey: ["generation-status", projectTag] });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectTag, cardIdentities, serverUnavailable, adoptNonce]);

  // Languages the import dropdown may request — the full Scryfall list
  // (server-owned, same rule as the models list), independent of what
  // corpus is imported: the dropdown expresses what the user *wants*, and
  // resolution answers from the corpus or the live API. ["en"] only as
  // the offline placeholder.
  const languagesQuery = useQuery({
    queryKey: ["card-languages"],
    queryFn: () => generationApi.cardLanguages(),
    enabled: !serverUnavailable,
    staleTime: 5 * 60_000,
  });
  const languages = languagesQuery.data?.languages ?? ["en"];

  // Rarity colors each row's set symbol, and is stored with the card
  // (import, re-resolve and printing picks all save it) so the colors are
  // there on load rather than after the server finishes booting. Rows
  // saved before that — or pinned by a server too old to send it — are
  // filled in once from the corpus and written back. Attempted ids are
  // remembered per project so a printing the corpus has no rarity for
  // isn't asked about on every render.
  const rarityAttempted = useRef<Set<string>>(new Set());
  // A card-database update can bring what wasn't there before (flavor
  // names arrive only with a corpus imported after they were kept), so
  // a finished import asks about every card again. Same query as the
  // sidebar's Card database panel, which refreshes it when an import ends.
  const cardDbQuery = useQuery({
    queryKey: ["card-db-status"],
    queryFn: () => generationApi.cardDbStatus(),
    enabled: !serverUnavailable,
  });
  const corpusImportedAt = cardDbQuery.data?.local?.imported_at ?? null;
  useEffect(() => {
    rarityAttempted.current = new Set();
  }, [projectId, corpusImportedAt]);
  useEffect(() => {
    if (serverUnavailable || !serverSupportsSetIcons(serverVersion)) return;
    // Rows missing a rarity, or a flavor name (nearly every row: almost
    // no printing has one, so each id is asked about once a session).
    const missing = [
      ...new Set(
        cards
          .filter((c) => c.scryfall_id && (c.rarity == null || c.flavor_name == null))
          .map((c) => c.scryfall_id as string)
          .filter((id) => !rarityAttempted.current.has(id)),
      ),
    ];
    if (missing.length === 0) return;
    missing.forEach((id) => rarityAttempted.current.add(id));
    const lacksRarity = new Set(
      cards.filter((c) => c.scryfall_id && c.rarity == null).map((c) => c.scryfall_id as string),
    );
    generationApi
      .cardRarities(missing)
      .then((r) =>
        backfillRarities(
          Object.fromEntries(Object.entries(r.rarities).filter(([id]) => lacksRarity.has(id))),
          r.flavor_names ?? {},
        ),
      )
      // A failed request is retried the next time the cards change.
      .catch(() => missing.forEach((id) => rarityAttempted.current.delete(id)));
  }, [cards, serverUnavailable, serverVersion, backfillRarities, corpusImportedAt]);

  // On-load re-resolve for legacy rows only: cards imported before the
  // resolve-gated import existed (or by older builds) may be unpinned —
  // best-effort, relaxed (non-strict) resolution pins them. New imports
  // always arrive pinned, so this is a no-op for them.
  // attempted-set so a card that genuinely fails to resolve (typo) doesn't
  // refire on every render; reset per project.
  const resolveAttempted = useRef<Set<number>>(new Set());
  useEffect(() => {
    resolveAttempted.current = new Set();
  }, [projectId]);
  useEffect(() => {
    if (!projectTag || serverUnavailable) return;
    const unresolved = cards.filter(
      // custom_image_id excluded deliberately: a Custom Image card has no
      // scryfall_id and never will, so without this guard every one of
      // them would look like a legacy unpinned row and get sent to
      // /api/resolve on every project load — where at best it matches
      // nothing, and at worst its filename matches a real card and this
      // effect overwrites the user's own art with a Scryfall printing.
      (c) => !c.scryfall_id && !c.custom_image_id && !resolveAttempted.current.has(c.id),
    );
    if (unresolved.length === 0) return;
    unresolved.forEach((c) => resolveAttempted.current.add(c.id));
    let cancelled = false;
    generationApi
      .resolve(unresolved.map(cardToEntry))
      .then((result) => {
        if (cancelled) return;
        const byLine = new Map(unresolved.map((c) => [c.original_import_line, c]));
        const updates = result.resolved.flatMap((rc) => {
          const card = byLine.get(rc.raw_line);
          const face = rc.faces[0];
          if (!card || !face) return [];
          return [
            {
              card_id: card.id,
              scryfall_id: face.scryfall_id,
              name: face.card_name,
              set_code: face.set_code,
              collector_number: face.collector_number,
              lang: face.lang ?? "en",
              printed_name: face.printed_name ?? null,
              flavor_name: face.flavor_name ?? null,
              rarity: face.rarity ?? null,
            },
          ];
        });
        if (updates.length > 0) return applyCardResolutions(updates);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectTag, serverUnavailable, cards]);

  // Custom Image drop/pick. A counter rather than a boolean because drag
  // events fire for every child element entered — a plain flag flickers
  // off the moment the pointer crosses a card row (see BacksPage, which
  // learned this the same way).
  const [dragDepth, setDragDepth] = useState(0);
  const customFileInput = useRef<HTMLInputElement>(null);
  const [customUploadError, setCustomUploadError] = useState<string | null>(null);
  const [customProgress, setCustomProgress] = useState<{ done: number; total: number } | null>(
    null,
  );

  // MPC Autofill order import: every front and back the order uses goes
  // into the libraries through the server (one at a time, with a cancel),
  // then the order's slots become custom cards with their quantities and
  // backs. The order's default back is offered as the project's.
  const orderFileInput = useRef<HTMLInputElement>(null);
  const orderCanceled = useRef(false);
  const [orderProgress, setOrderProgress] = useState<{
    done: number;
    total: number;
    name: string;
  } | null>(null);
  const [orderNote, setOrderNote] = useState<string | null>(null);
  const [orderCardback, setOrderCardback] = useState<BackImage | null>(null);
  const mpcSupported = serverSupportsMpcFill(serverVersion);

  // The import toggle's "MPC Fill": every face of each newly imported
  // card gets MPC Fill's top match as its face art, downloaded one at a
  // time with a cancel. Faces with no match keep their Scryfall art.
  const [findingArt, setFindingArt] = useState(false);
  const [artNote, setArtNote] = useState<string | null>(null);
  async function pickMpcArtFor(imported: CardRow[]) {
    setFindingArt(true);
    try {
      const result = await autoPickMpcArt(imported, {
        setFaceArtPending: setCardFaceArtPending,
        // The import's own language: MPC Fill art in it, else the card
        // keeps the printing it was imported as.
        lang: settings.lang_any ? null : settings.preferred_lang,
      });
      // Art fetched since the library was last read (picks, Download
      // images) is linked to these new cards straight away, without a
      // download — see the library effect below.
      void queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      // Only what needs the user's attention.
      setArtNote(
        result.unmatched.length
          ? `No MPC Fill art for ${result.unmatched.join(", ")} — kept Scryfall art.`
          : null,
      );
    } catch (err) {
      setArtNote(
        `Couldn't pick MPC Fill art (${err instanceof Error ? err.message : String(err)}) — kept Scryfall art.`,
      );
    } finally {
      setFindingArt(false);
    }
  }
  const importOrderMutation = useMutation({
    mutationFn: async (file: File) => {
      const order = parseMpcOrder(await file.text());
      orderCanceled.current = false;
      setOrderProgress({ done: 0, total: order.fronts.length + order.backs.length, name: "" });
      const result = await importMpcOrder(
        order,
        (done, total, name) => setOrderProgress({ done, total, name }),
        () => orderCanceled.current,
      );
      if (result.canceled) return { ...result, added: 0, warnings: order.warnings };
      let added = 0;
      if (result.items.length) {
        const out = await addCustomCardsWithBacks(result.items);
        added = result.items.reduce((sum, item) => sum + item.quantity, 0);
        if (!serverUnavailable) {
          void registerCustomCards(out.cards, out.projectTag, serverVersion)
            .then(() => invalidateStatus())
            .catch(() => {});
        }
      }
      return { ...result, added, warnings: order.warnings };
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      void queryClient.invalidateQueries({ queryKey: ["back-images"] });
      setOrderProgress(null);
      const problems = [...result.warnings, ...result.errors];
      setOrderNote(
        result.canceled
          ? "Import canceled; art already fetched stays in the library."
          : `Added ${result.added} card(s) from the order.${
              problems.length ? ` Not imported: ${problems.join(" ")}` : ""
            }`,
      );
      if (!result.canceled && result.cardback && result.cardback.id !== settings.back_image_id) {
        setOrderCardback(result.cardback);
      }
    },
    onError: (err: Error) => {
      setOrderProgress(null);
      setOrderNote(`Couldn't import that order: ${err.message}`);
    },
  });

  const addCustomImagesMutation = useMutation({
    mutationFn: async (files: File[]) => {
      setCustomProgress({ done: 0, total: files.length });
      const result = await addImagesSequentially(
        files,
        (file, picked) =>
          projectApi.addCustomImage({ ...picked, originalFilename: file.name }),
        (done, total) => setCustomProgress({ done, total }),
      );
      // One store write for the whole drop rather than one per file: a
      // 30-image drop should append 30 cards once, not re-render the list
      // 30 times.
      if (result.added.length) {
        const added = await addCustomCards(result.added.map((image) => image.id));
        // "Dropped it, it's ready to print": sync + register on the spot
        // when a server is reachable, so the PDF tab never asks for a
        // Download/Generate click first. Best-effort and off the critical
        // path — the add itself already succeeded, and an offline add
        // registers later (next add, Generate/Download, or the PDF/ZIP
        // export paths, which all call the same idempotent helper).
        if (!serverUnavailable) {
          void registerCustomCards(added.cards, added.projectTag, serverVersion)
            .then(() => invalidateStatus())
            .catch(() => {});
        }
      }
      return result;
    },
    onSuccess: ({ errors }) => {
      void queryClient.invalidateQueries({ queryKey: ["custom-images"] });
      setCustomUploadError(errors.length ? errors.join(" ") : null);
      setCustomProgress(null);
    },
    onError: (err: Error) => {
      setCustomUploadError(err.message);
      setCustomProgress(null);
    },
  });

  function handleCustomFiles(list: FileList | null) {
    const files = Array.from(list ?? []);
    if (files.length) addCustomImagesMutation.mutate(files);
  }

  // MPC Fill art an import chose but didn't download is fetched by the
  // same buttons that fetch a Scryfall import's images: Download images
  // queues it, Generate waits for it (it may be upscaled). Each face shows
  // its progress on its row.
  const pendingArtOpts = {
    setFaceArt: setCardFaceArt,
    projectTag,
    serverVersion,
    refreshStatus: refetchStatus,
  };
  // Chosen MPC art that is already in the library (downloaded for another
  // card, project or earlier pick) needs no download: link it straight
  // away, so the row shows it as downloaded on import or load rather than
  // waiting for Download images to find it there.
  const libraryQuery = useQuery({
    queryKey: ["custom-images"],
    queryFn: () => projectApi.listCustomImages(),
  });
  const linkingArt = useRef(new Set<string>());
  useEffect(() => {
    const byDriveId = new Map(
      (libraryQuery.data ?? []).flatMap((image) =>
        image.mpc ? [[image.mpc.drive_id, image] as const] : [],
      ),
    );
    if (byDriveId.size === 0) return;
    const links = cards.flatMap((card) =>
      (card.face_art ?? []).flatMap((fa) => {
        const image = fa.custom_image_id == null && fa.mpc_drive_id ? byDriveId.get(fa.mpc_drive_id) : undefined;
        const key = `${card.id}:${fa.face}:${fa.mpc_drive_id}`;
        if (!image || linkingArt.current.has(key)) return [];
        linkingArt.current.add(key);
        return [{ card, fa, image }];
      }),
    );
    if (links.length === 0) return;
    void (async () => {
      const linked: CardRow[] = [];
      for (const { card, fa, image } of links) {
        try {
          await setCardFaceArt(card.id, fa.face, fa.total_faces, image);
          linked.push({
            ...card,
            face_art: [
              {
                ...fa,
                custom_image_id: image.id,
                custom_hash: image.content_hash,
                label: image.label,
              },
            ],
          });
        } catch {
          // Left as chosen; Download images still fetches it.
        }
      }
      if (linked.length > 0 && !serverUnavailable) {
        await registerCustomCards(linked, projectTag, serverVersion).catch(() => {});
        await refetchStatus();
      }
    })();
    // setCardFaceArt/refetchStatus are stable enough; re-running on them
    // would only repeat the same (already-attempted) links.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cards, libraryQuery.data]);

  /** One row's Download: what Download images does, for this card only —
   *  its chosen MPC art, its Scryfall original(s) as a queued task (the
   *  row shows "Downloading · Original" until it lands), or a custom
   *  card's registration. Best-effort; errors surface on the next bulk
   *  action. */
  function downloadCard(card: CardRow) {
    void fetchPendingFaceArt([card], pendingArtOpts);
    if (card.custom_image_id != null) {
      void registerCustomCards([card], projectTag, serverVersion)
        .then(() => refetchStatus())
        .catch((err: unknown) =>
          setStatus(`Download failed: ${err instanceof Error ? err.message : String(err)}`),
        );
      return;
    }
    downloadPickedPrinting(card, projectTag, refetchStatus);
  }

  /** Fetch these cards' chosen MPC art, then the cards as they now are. */
  async function withPendingArtFetched(subset: CardRow[]): Promise<CardRow[]> {
    if (!hasPendingFaceArt(subset)) return subset;
    await fetchPendingFaceArt(subset, pendingArtOpts);
    const ids = new Set(subset.map((c) => c.id));
    return getProjectSnapshot().cards.filter((c) => ids.has(c.id));
  }

  // Library images that came from MPC Fill: what "Upscale MPC Fill" covers
  // among custom cards (face art says so itself).
  const mpcImageIds = useMemo(
    () => new Set((libraryQuery.data ?? []).flatMap((image) => (image.mpc ? [image.id] : []))),
    [libraryQuery.data],
  );

  const generateAllMutation = useMutation({
    mutationFn: async (scope: UpscaleScope) => {
      const first = scopeRequest(cards, scope, mpcImageIds);
      if (first.entries.length === 0) return null;
      const ready = await withPendingArtFetched(first.cards);
      const request = scopeRequest(ready, scope, mpcImageIds);
      if (request.entries.length === 0) return null;
      // Custom art has to be on the server before the tasks that will read
      // it are queued — see syncCustoms.ts.
      await syncCustomImages(request.cards, serverVersion);
      return generationApi.generate({
        project_tag: projectTag as string,
        entries: request.entries,
        model: settings.model,
        dpi_targets: settings.dpi_targets,
        skip_existing: settings.skip_existing,
        tile_size: settings.tile_size,
        custom_upscale: request.upscaleCustoms ? customResult : "off",
        scryfall_upscale: request.scryfallUpscale,
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
        image_origin: await resolveImageOrigin(queryClient),
      });
    },
    onSuccess: (result, scope) => {
      setStatus(
        result == null
          ? SCOPE_EMPTY[scope]
          : result.queued
            ? `Queued ${result.queued} task(s) — see the Tasks tab to monitor progress.`
            : `Nothing to do — every requested image already exists.${result.notes.length ? " " + result.notes.join(" ") : ""}`,
      );
      invalidateStatus();
    },
    onError: (err: Error) => {
      // Cancelling the custom-image upload is a normal outcome, not a failure.
      if (err instanceof UploadCanceled) return;
      setStatus(`Upscale failed: ${err.message}`);
    },
  });

  const downloadAllMutation = useMutation({
    mutationFn: async () => {
      // Chosen MPC Fill art downloads in the background queue, each face
      // registering itself as it lands (the rest needn't wait for it).
      void fetchPendingFaceArt(cards, pendingArtOpts);
      // Custom cards have no Scryfall original to download; this is what
      // registers their uploaded art as printable instead, so they still
      // need syncing first.
      await syncCustomImages(cards, serverVersion);
      return generationApi.downloadOriginals({
        project_tag: projectTag as string,
        entries: cards.map(cardToEntry),
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
        image_origin: await resolveImageOrigin(queryClient),
      });
    },
    onSuccess: (result) => {
      setStatus(
        result.queued
          ? `Queued ${result.queued} download(s) — see the Tasks tab to monitor progress.`
          : `Nothing to do — every original is already downloaded.${result.notes.length ? " " + result.notes.join(" ") : ""}`,
      );
      invalidateStatus();
    },
    onError: (err: Error) => {
      // Cancelling the custom-image upload is a normal outcome, not a failure.
      if (err instanceof UploadCanceled) return;
      setStatus(`Download failed: ${err.message}`);
    },
  });

  const generateCardMutation = useMutation({
    mutationFn: async (picked: CardRow) => {
      const [card = picked] = await withPendingArtFetched([picked]);
      await syncCustomImages([card], serverVersion);
      return generationApi.generate({
        project_tag: projectTag as string,
        entries: [cardToEntry(card)],
        model: settings.model,
        dpi_targets: settings.dpi_targets,
        skip_existing: settings.skip_existing,
        tile_size: settings.tile_size,
        // A card's own button upscales everything on it, whatever kind.
        custom_upscale: customResult,
        scryfall_upscale: true,
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
        image_origin: await resolveImageOrigin(queryClient),
      });
    },
    onSuccess: (result) => {
      // Only a skip needs saying: queued work shows on the row's badges.
      if (result.queued === 0 && result.notes.length) setStatus(result.notes.join(" "));
      invalidateStatus();
    },
    onError: (err: Error) => {
      // Cancelling the custom-image upload is a normal outcome, not a failure.
      if (err instanceof UploadCanceled) return;
      setStatus(`Upscale failed: ${err.message}`);
    },
  });

  const regenerateMutation = useMutation({
    mutationFn: async (galleryItemId: number) =>
      generationApi.regenerateGalleryItem(galleryItemId, {
        // Gallery rows are global registry entries shared across
        // projects — the request says whose regeneration this is.
        project_tag: projectTag as string,
        tile_size: settings.tile_size,
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
        image_origin: await resolveImageOrigin(queryClient),
      }),
    onSuccess: invalidateStatus,
    onError: (err: Error) => setStatus(`Regenerate failed: ${err.message}`),
  });

  const refetchMutation = useMutation({
    mutationFn: async (galleryItemId: number) =>
      generationApi.refetchOriginal(galleryItemId, {
        project_tag: projectTag as string,
        output_dir: DEFAULT_GEN_PATHS.output_dir,
        cache_dir: DEFAULT_GEN_PATHS.cache_dir,
        weights_dir: DEFAULT_GEN_PATHS.weights_dir,
        image_origin: await resolveImageOrigin(queryClient),
      }),
    onSuccess: invalidateStatus,
    onError: (err: Error) => setStatus(`Re-fetch failed: ${err.message}`),
  });

  // This tab's own shortcuts: Cmd/Ctrl+Enter imports the pasted decklist,
  // +G generates all, +D downloads all. Registered by this page, so unlike
  // the tab switcher and New (App.tsx, ProjectBar.tsx) they exist only
  // while the Decklist tab is showing — and each fires only when its
  // button could be clicked. The latest-closure ref keeps one listener
  // for the page's life while every action sees current state.
  const shortcutActions = useRef<Record<string, () => void>>({});
  useEffect(() => {
    shortcutActions.current = {
      Enter: () => {
        if (decklistDraft.trim() && !importing && !serverUnavailable) void handleImport();
      },
      KeyG: () => {
        if (cards.length && !generateAllMutation.isPending && !serverUnavailable) {
          generateAllMutation.mutate("originals");
        }
      },
      KeyD: () => {
        if (cards.length && !downloadAllMutation.isPending && !serverUnavailable) {
          downloadAllMutation.mutate();
        }
      },
    };
  });
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!hasShortcutModifier(e) || e.shiftKey) return;
      const action = shortcutActions.current[e.code === "NumpadEnter" ? "Enter" : e.code];
      if (!action) return;
      // Claimed even when ignored: the webview's own Cmd+G (find next) and
      // Cmd/Ctrl+D (bookmark) must never fire on this tab.
      e.preventDefault();
      if (isDialogOpen()) return;
      action();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // ConfirmDialog rather than a checkbox-arm: destructive actions ask
  // their yes/no question in a modal (see ConfirmDialog.tsx for why not
  // window.confirm).
  const [confirmClearOpen, setConfirmClearOpen] = useState(false);
  const clearGeneratedMutation = useMutation({
    mutationFn: () =>
      generationApi.clearGeneratedData(
        DEFAULT_GEN_PATHS.output_dir,
        DEFAULT_GEN_PATHS.cache_dir,
        projectTag ?? undefined,
      ),
    onSuccess: () => {
      invalidateStatus();
    },
    onError: (err: Error) => setStatus(`Clear failed: ${err.message}`),
  });

  function openDirectory(label: string, path: string) {
    // Local: OS file manager. Remote: a terminal window ssh'd into the
    // server and cd'd to the directory — deliberately not an sftp:// URL
    // handed to the OS, whose scheme handler is a lottery (VLC commonly
    // claims sftp:// on Linux and "opens" the directory as a playlist).
    // A "remote" server on this very machine (the server app + client
    // pairing) has its directories on the local disk: open them locally
    // rather than ssh-ing into ourselves.
    const viaSsh = connection.mode === "remote" && !isLoopbackHost(connection.host);
    const opening = viaSsh
      ? invokeOpenRemoteTerminal(connection.host, path)
      : invokeOpenDirectory(path);
    opening.catch((err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      const hint = viaSsh ? " — copy the path and ssh in manually instead." : "";
      setStatus(`Couldn't open ${label.toLowerCase()}: ${msg}${hint}`);
    });
  }

  // Button for one generation directory — opens it (file manager
  // locally, an SSH terminal for a remote server). The resolved absolute
  // path lives in the tooltip rather than the button face; disabled
  // until the server has reported it. Plain text in a browser dev tab,
  // where there's no Tauri command to invoke.
  function dirRow(label: string, path: string | undefined) {
    if (!isTauri()) {
      return (
        <span className="path-text mono">
          {label}: {path ?? "…"}
        </span>
      );
    }
    const title =
      path == null
        ? "Waiting for the server…"
        : connection.mode === "remote"
          ? `${path} — opens an SSH terminal on the server`
          : path;
    return (
      <button
        type="button"
        className="path-link"
        disabled={path == null}
        title={title}
        onClick={() => path != null && openDirectory(label, path)}
      >
        <span>{label}</span>
        <span className="path-open-icon" aria-hidden="true">
          ↗
        </span>
      </button>
    );
  }

  function toggleExpanded(key: string) {
    setExpandedFaces((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const tasks: Task[] = statusQuery.data?.tasks ?? [];
  const gallery: GalleryItem[] = statusQuery.data?.gallery ?? [];
  const { galleryByCard, tasksByCard } = groupByCard(gallery, tasks);
  const { galleryByName, tasksByName } = groupByCardName(gallery, tasks);

  const sortedCards = sortCards(cards, settings.sort_primary);

  return (
    <div className="layout">
      <aside className="sidebar panel">
        <h3 style={{ marginBottom: 14 }}>Settings</h3>

        {/* Above the field group, and never behind a project check:
            switching to an empty server leaves projectId null, and if this
            were gated on a project there'd be no way to switch back. */}
        <ServerSwitcher />

        <div className="field-group">
          <UpscaleSettingsFields
            value={settings}
            onChange={(patch) => setSettings((s) => ({ ...s, ...patch }))}
          />

          <label className="check">
            <input
              type="checkbox"
              checked={settings.skip_existing}
              onChange={(e) => setSettings((s) => ({ ...s, skip_existing: e.target.checked }))}
            />
            Skip existing output files
          </label>

          {/* How custom and MPC Fill art comes out when an upscale button
              asks for it — the buttons decide whether. Either way only art
              below a selected target is upscaled, and one too large for
              this machine is refused with a message on its task
              (proxy_scaler/upscale.py); it still prints at its own
              resolution. */}
          <label className="field" data-tour="custom-upscale-select">
            <span>Custom & MPC Fill Upscales</span>
            <SelectMenu
              options={[
                { value: "target", label: "Match target DPI" },
                { value: "native", label: "Keep full 4× (up to 2400 DPI)" },
              ]}
              value={customResult === "native" ? "native" : "target"}
              disabled={!customUpscaleSupported}
              onChange={(value) =>
                setSettings((s) => ({ ...s, custom_upscale: value as CustomUpscaleMode }))
              }
            />
          </label>
          <p className="hint">
            {customUpscaleSupported
              ? "Used by Upscale Customs, Upscale MPC Fill, Upscale Everything and a card's own upscale button. Art already within 10% of a target is left as it is."
              : "Needs a newer generation server. Custom and MPC Fill art print as they are."}
          </p>

          <div className="field divided">
            <span>Directories</span>
            {dirRow("Output", pathsQuery.data?.output_dir)}
            {dirRow("Cache", pathsQuery.data?.cache_dir)}
            {dirRow("Weights", pathsQuery.data?.weights_dir)}
          </div>

          <CardDbPanel serverUnavailable={serverUnavailable} />
          <MpcCatalogPanel serverUnavailable={serverUnavailable} />
        </div>

        <div className="danger-zone">
          <h3>Danger zone</h3>
          <p className="hint">
            Deletes the generated images and download cache (the output and cache directories
            above). Model weights and uploaded custom images are kept.
          </p>
          <button
            className="btn-danger btn-block"
            onClick={() => setConfirmClearOpen(true)}
            disabled={clearGeneratedMutation.isPending}
          >
            Delete all generated images &amp; cache
          </button>
          {confirmClearOpen && (
            <ConfirmDialog
              title="Delete all generated images & cache?"
              confirmLabel="Delete everything"
              onConfirm={() => {
                setConfirmClearOpen(false);
                clearGeneratedMutation.mutate();
              }}
              onCancel={() => setConfirmClearOpen(false)}
            >
              This deletes every generated image and the download cache (the
              output and cache directories in the sidebar). Model weights and
              your uploaded custom images are kept. Cards can be re-generated
              any time.
            </ConfirmDialog>
          )}
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

      {/* The whole content area is the drop target, not just the
          dropzone tile — dropping an image anywhere over the decklist is
          what people try first. Only works at all because the main window
          sets dragDropEnabled: false (tauri.conf.json); Tauri v2 defaults
          it to true, which makes the native layer swallow OS file drops
          before the webview ever sees them, with no error to explain
          why. */}
      <main
        className="content"
        onDragEnter={(e) => {
          e.preventDefault();
          setDragDepth((d) => d + 1);
        }}
        onDragOver={(e) => {
          // Without preventDefault the browser treats the drop as
          // navigation and opens the image instead.
          e.preventDefault();
        }}
        onDragLeave={() => setDragDepth((d) => Math.max(0, d - 1))}
        onDrop={(e) => {
          e.preventDefault();
          setDragDepth(0);
          handleCustomFiles(e.dataTransfer.files);
        }}
      >
        <h2>Decklist</h2>

        {/* The import box is unconditional: with no project yet, importing
            is what creates one (see ProjectContext's importDecklistText).
            Gating it on projectId would be circular — the row is born at
            first import, so the box that performs the import can't wait for
            the row. The readiness note that follows is about the local
            generation server, which importing doesn't touch — so it sits
            beside the box rather than in place of it. */}
        {readiness.status === "starting" && (
          <p className="hint" style={{ marginTop: 10 }}>
            Starting local server — your last project will load automatically.
          </p>
        )}

        <div className="import-box panel" style={{ marginTop: 10 }} data-tour="import-box">
          <p className="hint">
            One card per line — best format:{" "}
            <code>4 Card Name (set) 123</code>. Set and collector number are
            optional; <code>4x</code> works too.
          </p>
          <textarea
            value={decklistDraft}
            onChange={(e) => setDecklistDraft(e.target.value)}
            rows={6}
            style={{ width: "100%" }}
            placeholder={"1 Sol Ring (c21) 263\n2 Arcane Signet 117\n4 Lightning Bolt"}
          />
          <div className="import-actions import-actions-wide">
            <button
              className="btn-primary"
              onClick={() => void handleImport()}
              disabled={!decklistDraft.trim() || importing || serverUnavailable}
              title={
                serverUnavailable
                  ? "Generation server is required to import"
                  : shortcutLabel("Enter")
              }
            >
              {importing ? "Importing…" : "Import cards"}
            </button>
            {/* The language this import demands ("strictly literal" — a
                card without a version in this language errors and is not
                added), unless "All Languages" hands matching back to
                best-effort. Options are the full Scryfall list, served by
                the server regardless of which corpus is imported. */}
            <SelectMenu
              ariaLabel="Card language to import"
              title="Card language to import"
              options={(languages.includes(settings.preferred_lang)
                ? languages
                : [settings.preferred_lang, ...languages]
              ).map((lang) => ({ value: lang, label: lang.toUpperCase() }))}
              value={settings.preferred_lang}
              disabled={settings.lang_any}
              onChange={(value) => setSettings((s) => ({ ...s, preferred_lang: value }))}
            />
            <label className="check" title="Match any language, best effort">
              <input
                type="checkbox"
                checked={settings.lang_any}
                onChange={(e) =>
                  setSettings((s) => ({ ...s, lang_any: e.target.checked }))
                }
              />
              All Languages
            </label>
            {/* Where imported cards' art comes from. MPC Fill still matches
                each line against Scryfall (that's the card), then prints
                MPC Fill's top art for every face in its place. */}
            <span className="import-art-source">
              <span className="hint">Art From</span>
              <SplitPill
                ariaLabel="Where imported cards' art comes from"
                value={settings.import_art_source}
                onChange={(value) => setSettings((s) => ({ ...s, import_art_source: value }))}
                options={[
                  {
                    value: "scryfall",
                    label: "Scryfall",
                    title: "Each card's own printing (upscaled when you Generate)",
                  },
                  {
                    value: "mpc",
                    label: "MPC Fill",
                    title: mpcSupported
                      ? "MPC Fill's top community art for every face, picked automatically on import. Change any of them from the card's printing button."
                      : "Needs a newer generation server",
                    disabled: !mpcSupported,
                  },
                ]}
              />
            </span>
          </div>
          {findingArt && <p className="hint">Finding MPC Fill art…</p>}
          {artNote && <p className="hint">{artNote}</p>}
          {serverUnavailable && (
            <p className="hint">
              Importing needs the generation server — cards are matched
              before they're added.
            </p>
          )}
          {status && <p className="hint">{status}</p>}
          {importFailures.length > 0 && (
            <ul className="import-failures">
              {importFailures.map((f) => (
                <li key={f.line} className="error-text">
                  <span className="mono">{f.line}</span> — {f.error}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Adding your own art, beside the decklist box rather than on a
            tab of its own, because the result is the same thing: cards in
            this project.

            Deliberately NOT disabled when the server is unreachable,
            unlike the import button above. Importing a decklist has to
            match each line against Scryfall before it can add anything; an
            uploaded image has nothing to resolve, so this is the one way
            into a project that works with no server at all. */}
        <div
          className={`import-box panel custom-drop${dragDepth > 0 ? " is-dragging" : ""}`}
          style={{ marginTop: 10 }}
          data-tour="custom-drop"
        >
          <p className="hint">
            Or use your own art — drop image files anywhere on this page, or{" "}
            <button
              type="button"
              className="linklike"
              onClick={() => customFileInput.current?.click()}
              disabled={addCustomImagesMutation.isPending}
            >
              choose files
            </button>
            . Each becomes a card named after its file. PNG, JPEG or WebP up to{" "}
            {MAX_UPLOAD_MB}MB each.
          </p>
          <input
            ref={customFileInput}
            type="file"
            multiple
            accept={ACCEPTED_IMAGE_TYPES}
            style={{ display: "none" }}
            onChange={(e) => {
              // Copy before resetting: picking the same file twice in a
              // row fires no change event otherwise, which reads as the
              // control being broken.
              const files = e.target.files ? Array.from(e.target.files) : [];
              e.target.value = "";
              if (files.length) addCustomImagesMutation.mutate(files);
            }}
          />
          {customProgress && addCustomImagesMutation.isPending && (
            <p className="hint">
              Adding {Math.min(customProgress.done + 1, customProgress.total)} of{" "}
              {customProgress.total}…
            </p>
          )}
          {customUploadError && <p className="error-text">{customUploadError}</p>}
          <p className="hint">
            Have an MPC Autofill order?{" "}
            <button
              type="button"
              className="linklike"
              onClick={() => orderFileInput.current?.click()}
              disabled={importOrderMutation.isPending || serverUnavailable || !mpcSupported}
              title={
                serverUnavailable
                  ? "Generation server is unreachable"
                  : !mpcSupported
                    ? "Needs a newer generation server"
                    : "Import the .xml file MPC Fill's Download XML button saves"
              }
            >
              Import the order (.xml)
            </button>
            . Its art is fetched into your Custom and Back libraries.
          </p>
          <input
            ref={orderFileInput}
            type="file"
            accept=".xml,text/xml,application/xml"
            style={{ display: "none" }}
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) {
                setOrderNote(null);
                importOrderMutation.mutate(file);
              }
            }}
          />
          {orderProgress && importOrderMutation.isPending && (
            <p className="hint">
              Fetching art {Math.min(orderProgress.done + 1, orderProgress.total)} of{" "}
              {orderProgress.total}
              {orderProgress.name ? ` — ${orderProgress.name}` : ""}…{" "}
              <button
                type="button"
                className="linklike"
                onClick={() => {
                  orderCanceled.current = true;
                }}
              >
                Cancel
              </button>
            </p>
          )}
          {orderNote && <p className="hint">{orderNote}</p>}
          {orderCardback && (
            <ConfirmDialog
              title="Use the order's card back?"
              confirmLabel="Use this back"
              tone="primary"
              onConfirm={() => {
                const backId = orderCardback.id;
                setSettings((s) => ({ ...s, back_image_id: backId }));
                setOrderCardback(null);
              }}
              onCancel={() => setOrderCardback(null)}
            >
              The order prints &ldquo;{orderCardback.label}&rdquo; on the back of every card
              without its own. Make it this project&apos;s back?
            </ConfirmDialog>
          )}
        </div>

        <div className="decklist-head">
          <h2>
            Cards <span style={{ color: "var(--text-faint)" }}>({cards.length})</span>
          </h2>
          <div className="decklist-actions" data-tour="deck-actions">
            <SortSelect />
            {ownBackCount > 0 && (
              <button
                onClick={() => setConfirmResetBacks(true)}
                title="Set every card back to the universal back"
              >
                Reset backs ({ownBackCount})
              </button>
            )}
            <button
              onClick={() => downloadAllMutation.mutate()}
              disabled={!cards.length || downloadAllMutation.isPending || serverUnavailable}
              title={
                serverUnavailable
                  ? "Generation server is unreachable"
                  : `Download the ~300 DPI Scryfall originals without upscaling · ${shortcutLabel("D")}`
              }
            >
              Download images
            </button>
            <SplitButton
              primary
              label={SCOPE_LABELS.originals}
              onClick={() => generateAllMutation.mutate("originals")}
              options={(["customs", "mpc", "all"] as const).map((scope) => ({
                key: scope,
                label: SCOPE_LABELS[scope],
              }))}
              onPick={(scope) => generateAllMutation.mutate(scope as UpscaleScope)}
              disabled={!cards.length || generateAllMutation.isPending || serverUnavailable}
              title={
                serverUnavailable
                  ? "Generation server is unreachable"
                  : `Upscale the Scryfall art to the selected DPIs · ${shortcutLabel("G")}`
              }
            />
          </div>
        </div>

        {serverUnavailable && (
          <p className="error-text" style={{ marginTop: 6 }}>
            Generation server is unreachable — reconnect before generating.
          </p>
        )}

        {cards.length === 0 && (
          <p className="empty-note">
            No cards yet — paste a decklist above and click Import cards.
          </p>
        )}

        {sortedCards.map((card) => {
          const identity = localCardIdentity(card);
          let cardGallery = galleryByCard.get(identity) ?? [];
          let cardTasks = tasksByCard.get(identity) ?? [];
          if (card.custom_image_id == null && (!card.set_code || !card.collector_number)) {
            // Name-only local card (never a custom, which matches by
            // hash alone): the generation server always
            // resolves it to one concrete printing (a real set +
            // collector_number), so it can never land in the
            // identity bucket above — match by name instead. See
            // mergeCardStatus.ts::groupByCardName.
            const nameKey = card.name.toLowerCase();
            cardGallery = [...cardGallery, ...(galleryByName.get(nameKey) ?? [])];
            cardTasks = [...cardTasks, ...(tasksByName.get(nameKey) ?? [])];
          }
          const faceGroups = buildRows(cardGallery, cardTasks);
          const scryfallFaces = faceGroups
            .map(({ items, tasks: faceTasks }) => {
              const source = items[0] ?? faceTasks[0];
              return {
                faceIndex: source?.face_index ?? null,
                face: {
                  faceLabel: source?.face_label ?? null,
                  // Canceled tasks are just queue history, not worth
                  // surfacing here — the Tasks tab is where cancellation
                  // state actually matters.
                  variants: statusForPairs(items, faceTasks).filter(
                    (v) => v.status !== "canceled",
                  ),
                } as DisplayFace,
              };
            })
            .filter(({ face }) => face.variants.length > 0);
          // A face printing MPC Fill art shows that image's variants in
          // its place (they live under the image's own identity).
          const faces: DisplayFace[] = withPendingPicks(
            applyFaceArt(scryfallFaces, card.face_art ?? [], galleryByCard, tasksByCard),
            card.id,
            pendingArt,
          );
          // Known only once the card's faces have been generated or
          // downloaded: a double-faced card's "back" face is what prints
          // on its Reverse while the PDF tab pairs faces, so the per-card
          // back control shows locked for it (CardBackPicker).
          const transformOnBack =
            settings.back_printing &&
            settings.back_faces_as_reverse &&
            faces.some((f) => f.faceLabel === "back");
          return (
            <CardRowView
              key={card.id}
              card={card}
              faces={faces}
              transformOnBack={transformOnBack}
              expandedFaces={expandedFaces}
              onToggleExpand={toggleExpanded}
              onRemove={() => removeCard(card.id)}
              onSetQuantity={(quantity) => setCardQuantity(card.id, quantity)}
              onGenerate={() => generateCardMutation.mutate(card)}
              onDownload={() => downloadCard(card)}
              onRegenerate={(galleryItemId) => regenerateMutation.mutate(galleryItemId)}
              onRefetch={(galleryItemId) => refetchMutation.mutate(galleryItemId)}
              preferredLang={settings.preferred_lang}
              rarity={card.rarity ?? undefined}
              onPickPrinting={(printing) => {
                // Persisting the pick changes the card's identity string,
                // which re-fires the adopt effect above (badges re-bucket
                // to whatever exists for the new printing); the status
                // query refresh makes that visible without waiting for the
                // 3s poll.
                setCardPrinting(card.id, printing)
                  .then(() => {
                    // Fetch the picked printing's full-size image now, as
                    // a queued download task (the Download button's), so
                    // its "300 · Original" lands without another click.
                    downloadPickedPrinting(
                      {
                        ...card,
                        scryfall_id: printing.scryfallId,
                        name: printing.name,
                        set_code: printing.setCode,
                        collector_number: printing.collectorNumber,
                        lang: printing.lang,
                        printed_name: printing.printedName,
                        flavor_name: printing.flavorName ?? null,
                        face_art: [],
                      },
                      projectTag,
                      refetchStatus,
                    );
                  })
                  .catch(() => {});
              }}
              onPickerClosed={() => setAdoptNonce((n) => n + 1)}
              faceArt={{
                // Downloads in the background (pickDownloads.ts); the row
                // shows the face as downloading meanwhile.
                onPickArt: (face, totalFaces, art) => {
                  dismissPendingFaceArt(card.id, face);
                  void queueFaceArtPick({
                    card,
                    face,
                    totalFaces,
                    art,
                    setFaceArt: setCardFaceArt,
                    projectTag,
                    serverVersion,
                    refreshStatus: refetchStatus,
                  });
                },
                onClearFaceArt: async (face) => {
                  await clearCardFaceArt(card.id, face);
                  invalidateStatus();
                },
              }}
              disabled={serverUnavailable}
            />
          );
        })}
      </main>
      {confirmResetBacks && (
        <ConfirmDialog
          title="Reset per-card backs?"
          confirmLabel="Reset backs"
          onConfirm={() => {
            setConfirmResetBacks(false);
            void resetCardBacks();
          }}
          onCancel={() => setConfirmResetBacks(false)}
        >
          <p>
            {ownBackCount === 1 ? "1 card" : `${ownBackCount} cards`} will go back to the
            universal back. The backs themselves stay in the library.
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}

function slugify(name: string): string {
  return name.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "card";
}

async function handleDownloadImage(url: string, filename: string): Promise<void> {
  await runDownload(filename, { url });
}

// localhost / 127.x / ::1, with or without brackets or a port.
function isLoopbackHost(host: string): boolean {
  let h = host.trim().toLowerCase();
  const bracketed = h.match(/^\[([^\]]+)\](?::\d+)?$/);
  if (bracketed) h = bracketed[1];
  else if ((h.match(/:/g) ?? []).length === 1) h = h.replace(/:\d+$/, "");
  return h === "localhost" || h === "::1" || /^127(\.\d{1,3}){3}$/.test(h);
}

// A compare is always within one face: the variant whose Compare was
// clicked (`mine`, fixed on the right) against any other version of that
// face (`against`, switchable in the dialog). `options` is every version
// of the face except `mine` — the original plus every other model/DPI.
interface CompareTarget {
  cardName: string;
  mine: CompareSide;
  against: CompareSide;
  options: CompareSide[];
}

function CardRowView(props: {
  card: CardRow;
  faces: DisplayFace[];
  /** This card's transform side prints on its back — see CardBackPicker. */
  transformOnBack: boolean;
  expandedFaces: Set<string>;
  onToggleExpand: (key: string) => void;
  onRemove: () => void;
  onSetQuantity: (quantity: number) => void;
  onGenerate: () => void;
  /** Queue this card's full-size images (no upscaling), like the bulk
   *  Download images for one row. */
  onDownload: () => void;
  /** Whether Generate upscales Custom Images for this project — without
   *  it a custom's per-row Generate would upscale nothing. */
  onRegenerate: (galleryItemId: number) => void;
  /** Re-download this face's Scryfall original, overwriting the cached
   *  copy — takes any of the face's gallery item ids. */
  onRefetch: (galleryItemId: number) => void;
  /** The project's import-language preference — PrintingPicker's default
   *  language filter for cards that don't carry their own. */
  preferredLang: string;
  /** The pinned printing's rarity, for its set symbol's color. */
  rarity?: string;
  onPickPrinting: (printing: {
    scryfallId: string;
    name: string;
    setCode: string;
    collectorNumber: string;
    lang: string;
    printedName: string | null;
    flavorName?: string | null;
    rarity: string | null;
  }) => void;
  /** The printing picker closed (picked or not) — see adoptNonce. */
  onPickerClosed: () => void;
  /** MPC Fill art for this card's faces. */
  faceArt: FaceArtHandlers;
  /** True when the generation server is unreachable — disables
   *  Generate/Regen/printing changes (Remove/Show stay enabled since
   *  those are purely local). */
  disabled: boolean;
}) {
  const {
    card,
    faces,
    transformOnBack,
    expandedFaces,
    onToggleExpand,
    onRemove,
    onSetQuantity,
    onGenerate,
    onDownload,
    onRegenerate,
    onRefetch,
    preferredLang,
    rarity,
    onPickPrinting,
    onPickerClosed,
    faceArt,
    disabled,
  } = props;
  // A face tile's "Change art" opens the row's picker in MPC Fill mode.
  const [pickerRequest, setPickerRequest] = useState<PickerOpenRequest | null>(null);
  const quantity = card.quantity ?? 1;
  const rowKey = `card-${card.id}`;
  const expanded = expandedFaces.has(rowKey);
  // The "· CPU" marker on variants only makes sense when this machine's
  // server actually has a GPU — on a CPU-only box, CPU output is the
  // expected result, not a fault. null (probe not answered yet) must
  // never warn either (see config.ts's probedDevice contract).
  const probedDevice = useSyncExternalStore(subscribeProbedDevice, getProbedDevice);
  const warnCpuVariants = probedDevice?.kind === "gpu";
  // Includes variants mid-regeneration (old image still present) so the
  // Show/Hide toggle doesn't vanish while a Regen is in flight.
  const hasImages = faces.some((f) => f.variants.some((v) => v.galleryItemId != null));
  const [compareTarget, setCompareTarget] = useState<CompareTarget | null>(null);
  const [viewTarget, setViewTarget] = useState<{ title: string; src: string } | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  // Global single-flight lock (see download.ts::runDownload) -- disables
  // every Download button on the page the instant any one download
  // starts, not just once its file is ready. Without this, a slow fetch
  // gave zero feedback and a second click just launched another
  // unprotected download that looked like the first one "did nothing."
  const downloadStatus = useDownloadStatus();

  async function download(url: string, filename: string) {
    setDownloadError(null);
    try {
      await handleDownloadImage(url, filename);
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="card-row" data-tour="card-row">
      <div className="card-main">
        {/* The card's own name leads — it's what was imported and what
            everything else matches on — with what's printed on this
            printing beside it when that differs (a flavor name, or a
            non-English printing's name). */}
        <span className="card-name-cell">
          <span className="card-name">{card.name}</span>
          {(() => {
            const alt = alternateName(card.name, card.printed_name, card.flavor_name);
            return alt ? (
              <span className="card-alt-name" title="As printed on this card">
                {alt}
              </span>
            ) : null;
          })()}
        </span>
        {/* A Custom Image has no printings to choose between — it is one
            uploaded file. The chip holds the picker's place so the row
            still lines up with its neighbours. */}
        {card.custom_image_id != null ? (
          <span className="chip" title="Your own uploaded art — manage it on the Customs tab">
            Custom
          </span>
        ) : (
          <PrintingPicker
            card={card}
            preferredLang={preferredLang}
            rarity={rarity}
            disabled={disabled}
            onPick={onPickPrinting}
            onClosed={onPickerClosed}
            faceArt={faceArt}
            openRequest={pickerRequest}
          />
        )}
        <CardBackPicker card={card} transformOnBack={transformOnBack} />
        <span className="card-qty-stepper" data-tour="card-qty">
          <button
            className="btn-sm"
            aria-label="Decrease quantity"
            disabled={quantity <= 1}
            onClick={() => onSetQuantity(quantity - 1)}
          >
            −
          </button>
          <NumberInput
            value={quantity}
            min={1}
            onChange={(v) => onSetQuantity(Math.max(1, Math.round(v)))}
          />
          <button
            className="btn-sm"
            aria-label="Increase quantity"
            onClick={() => onSetQuantity(quantity + 1)}
          >
            +
          </button>
        </span>
        {/* Icons with tooltips: four labelled buttons no longer fit the
            row. Each slot is always present (disabled when it can't act)
            so the icons line up down the list. */}
        <span className="card-buttons" data-tour="card-buttons">
          <button
            className="btn-sm icon-button"
            onClick={() => onToggleExpand(rowKey)}
            disabled={!hasImages}
            title={
              hasImages
                ? expanded
                  ? "Hide images"
                  : "Show images"
                : "Nothing to show yet — download or generate this card first"
            }
            aria-label={expanded && hasImages ? "Hide images" : "Show images"}
          >
            {expanded && hasImages ? <EyeOffIcon /> : <EyeIcon />}
          </button>
          <button
            className="btn-sm icon-button"
            onClick={onDownload}
            disabled={disabled}
            title="Download — the full-size image, without upscaling"
            aria-label="Download"
          >
            <DownloadIcon />
          </button>
          {/* Upscales everything on this card, Scryfall faces and custom
              or MPC art alike, whatever the whole-deck buttons cover. */}
          <button
            className="btn-sm icon-button"
            onClick={onGenerate}
            disabled={disabled}
            title="Upscale — this card's art to the selected DPIs"
            aria-label="Upscale"
          >
            <SparklesIcon />
          </button>
          <button
            className="btn-sm btn-danger icon-button"
            onClick={onRemove}
            title="Remove from the deck"
            aria-label="Remove"
          >
            <TrashIcon />
          </button>
        </span>
      </div>

      {faces.length === 0 ? (
        <div className="empty-note">
          {card.custom_image_id != null
            ? "Not registered for printing yet — happens automatically once a generation server is reachable."
            : "Not Generated Yet"}
        </div>
      ) : (
        faces.map((face, i) => (
          <div key={i} className="variants" data-tour="card-variants">
            {face.faceLabel && <span className="variant-face">{face.faceLabel}</span>}
            {face.pick && (
              <StatusBadge
                status={
                  face.pick.status === "failed"
                    ? "failed"
                    : face.pick.status === "downloading"
                      ? "running"
                      : "pending"
                }
                title={face.pick.error ?? face.pick.name}
              >
                {face.pick.status === "failed"
                  ? "Download failed"
                  : face.pick.status === "downloading"
                    ? "Downloading"
                    : "Queued"}{" "}
                · {face.pick.source === "scryfall" ? "Original" : "MPC"}
              </StatusBadge>
            )}
            {/* Chosen MPC art not fetched yet: a plain note, like a Scryfall
                card's "Not Generated Yet", rather than a status badge. */}
            {face.art && !face.pick && face.variants.length === 0 && (
              <span className="variant-note" title="Download images or Generate fetches it">
                Not Downloaded Yet
              </span>
            )}
            {face.variants.map((v) => (
              <StatusBadge key={`${v.dpi}-${v.model}`} status={v.status}>
                {v.dpi} ·{" "}
                {face.art && v.model === CUSTOM_SOURCE_MODEL
                  ? face.art.mpc_drive_id
                    ? "MPC"
                    : "Custom"
                  : modelDisplayName(v.model)}
              </StatusBadge>
            ))}
          </div>
        ))
      )}

      {expanded &&
        faces.map((face, i) => {
          // Anything with a gallery item renders — including a variant
          // whose regeneration is currently queued/running (statusForPairs
          // keeps the old item's id then), so the existing image stays
          // visible instead of vanishing mid-regen.
          const doneVariants = face.variants.filter(
            (v): v is VariantStatus & { galleryItemId: number } =>
              v.galleryItemId != null,
          );
          // The original (pre-upscale) source image is shared across
          // every DPI/model variant of this face — keyed server-side by
          // scryfall_id/face_index only (see original_cache_path in
          // upscale.py), not by model/dpi — so any one "done" variant's
          // gallery_item_id resolves to the same underlying file via
          // GET /api/gallery/{id}/original. Shown once per face, not
          // once per variant. A download-only variant (ORIGINAL_MODEL)
          // is preferred as the source when present, and excluded from
          // the per-variant thumbs below — its "full" image IS the
          // original, so a "300 DPI · Original" tile with Compare/Regen
          // would be nonsense.
          // custom_source is a source-tier variant exactly like a
          // download-only original — its "full" image IS the uploaded
          // file, so a per-variant tile with Compare/Regen would compare
          // it against itself.
          const sourceModels = [ORIGINAL_MODEL, CUSTOM_SOURCE_MODEL] as string[];
          const originalVariant = doneVariants.find((v) => sourceModels.includes(v.model));
          const upscaleVariants = doneVariants.filter((v) => !sourceModels.includes(v.model));
          const originalSource = originalVariant ?? upscaleVariants[0];
          // Every comparable version of this face, in display order:
          // the original, then each upscale (any model, any DPI).
          const sides: CompareSide[] = [
            ...(originalSource
              ? [
                  {
                    key: "original",
                    label: "Original",
                    url: generationApi.imageUrl(originalSource.galleryItemId, "original"),
                  },
                ]
              : []),
            ...upscaleVariants.map((v) => ({
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
            setCompareTarget({ cardName: card.name, mine, against, options });
          };
          return (
            <div key={i} className="thumbs">
              {originalSource && (
                <div className="thumb">
                  <div className="thumb-label">Original</div>
                  <img
                    src={generationApi.imageUrl(originalSource.galleryItemId, "original")}
                    alt={card.name}
                    loading="lazy"
                  />
                  <div className="thumb-buttons">
                    <button
                      className="btn-sm"
                      disabled={downloadStatus != null}
                      onClick={() =>
                        download(
                          generationApi.imageUrl(originalSource.galleryItemId, "original"),
                          `${slugify(card.name)}-original.png`,
                        )
                      }
                    >
                      {downloadStatus ? "Downloading…" : "Download"}
                    </button>
                    <button
                      className="btn-sm"
                      onClick={() =>
                        setViewTarget({
                          title: `${card.name}${face.faceLabel ? ` (${face.faceLabel})` : ""} — Original`,
                          src: generationApi.imageUrl(originalSource.galleryItemId, "original"),
                        })
                      }
                    >
                      View
                    </button>
                    {/* Nothing to re-fetch from for a Custom Image: its
                        source is the file the user uploaded, and the way
                        to replace it is to upload a different one. The
                        server rejects the call outright; hiding the
                        button is how the user finds that out first. */}
                    {face.art ? (
                      <>
                        <button
                          className="btn-sm"
                          onClick={() => setPickerRequest({ source: "mpc", face: face.art!.face })}
                          disabled={disabled}
                          title="Pick different MPC Fill art for this face"
                        >
                          Change art
                        </button>
                        <button
                          className="btn-sm"
                          onClick={() => void faceArt.onClearFaceArt(face.art!.face)}
                          title="Print this face from the printing's own art again"
                        >
                          Use Scryfall art
                        </button>
                      </>
                    ) : card.custom_image_id == null ? (
                      <button
                        className="btn-sm"
                        onClick={() => onRefetch(originalSource.galleryItemId)}
                        disabled={disabled}
                        title="Re-download the original from Scryfall (overwrites the cached copy)"
                      >
                        Re-Fetch
                      </button>
                    ) : null}
                  </div>
                </div>
              )}
              {upscaleVariants.map((v) => (
                <div key={`${v.dpi}-${v.model}`} className="thumb">
                  <div className="thumb-label" title={`${v.dpi} DPI · ${v.model}`}>
                    {v.dpi} DPI · {modelDisplayName(v.model)}
                    {v.status !== "done" && (
                      <span className="thumb-regen-note">
                        {" "}
                        · {v.status === "running" ? "regenerating…" : "queued"}
                      </span>
                    )}
                    {warnCpuVariants && v.device === "cpu" && (
                      <span
                        className="thumb-cpu-note"
                        title="Generated on the CPU — the GPU ran out of memory during this generation. Regen to retry on the GPU."
                      >
                        {" "}
                        · CPU
                      </span>
                    )}
                  </div>
                  <img
                    src={generationApi.imageUrl(v.galleryItemId, "thumb")}
                    alt={card.name}
                    loading="lazy"
                  />
                  <div className="thumb-buttons">
                    <button
                      className="btn-sm"
                      disabled={downloadStatus != null}
                      onClick={() =>
                        download(
                          generationApi.imageUrl(v.galleryItemId, "full"),
                          `${slugify(card.name)}-${v.dpi}dpi-${v.model}.png`,
                        )
                      }
                    >
                      {downloadStatus ? "Downloading…" : "Download"}
                    </button>
                    {/* Compare = against the Original, as it always was;
                        the caret (only when there's more than one other
                        version) picks any other model/DPI of this face. */}
                    <span className="compare-split">
                      <button
                        className="btn-sm"
                        onClick={() => openCompare(`${v.dpi}:${v.model}`)}
                      >
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
                      onClick={() => onRegenerate(v.galleryItemId)}
                      disabled={disabled || v.status !== "done"}
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
            </div>
          );
        })}

      {downloadError && (
        <p className="error-text">
          Download failed: {downloadError}
        </p>
      )}

      {compareTarget && (
        <CompareDialog
          cardName={compareTarget.cardName}
          mine={compareTarget.mine}
          against={compareTarget.against}
          options={compareTarget.options}
          onChangeAgainst={(side) =>
            setCompareTarget((t) => (t ? { ...t, against: side } : t))
          }
          onClose={() => setCompareTarget(null)}
        />
      )}

      {viewTarget && (
        <FullImageDialog
          title={viewTarget.title}
          src={viewTarget.src}
          onClose={() => setViewTarget(null)}
        />
      )}
    </div>
  );
}
