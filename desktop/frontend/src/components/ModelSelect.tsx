import { Fragment } from "react";
import type { ModelOption } from "../api/types";

// The one model dropdown, rendered on the Decklist (generation model), PDF
// and Export (preferred model) pages. One component rather than three
// copies of the <select> because the grouping below must never drift
// between pages: the server marks each model with the header it sits
// under ("Models" for the torch runtime, "Vulkan Models" for ncnn), and
// every dropdown draws those as <optgroup>s in the server's order.
//
// A server older than the `group` field sends none: everything then lands
// in one unlabeled group, i.e. exactly the pre-grouping dropdown.

interface Props {
  value: string;
  onChange: (value: string) => void;
  models: ModelOption[] | undefined;
  disabled?: boolean;
  // Label for an extra leading option with value "" — the PDF/Export
  // pages' "Any (highest DPI available)". Absent on the generation picker.
  anyOption?: string;
}

export function groupModels(
  models: ModelOption[],
): { group: string | null; models: ModelOption[] }[] {
  const order: (string | null)[] = [];
  const byGroup = new Map<string | null, ModelOption[]>();
  for (const m of models) {
    const group = m.group ?? null;
    if (!byGroup.has(group)) {
      byGroup.set(group, []);
      order.push(group);
    }
    byGroup.get(group)!.push(m);
  }
  return order.map((group) => ({ group, models: byGroup.get(group)! }));
}

// The tile_size a model switch should leave behind. Every model tiles by
// the server's "GPU VRAM" tiers (torch models also have Auto = 0). Within
// one runtime the chosen tier carries over; across runtimes it resets to
// the new model's default (Auto for torch, Medium for Vulkan), because a
// tier picked for one engine's memory profile says nothing about the
// other's. A tile the new model doesn't list (a legacy manual number) is
// kept, and the dropdown shows it as "Custom".
export function reconcileTileForModel(
  next: ModelOption | undefined,
  prev: ModelOption | undefined,
  tile: number,
): number {
  const presets = next?.tile_presets ?? [];
  if (presets.length === 0) return tile;
  const defaultTile = (
    presets.find((p) => p.key === next?.default_tile_preset) ?? presets[0]
  ).tile;
  if (prev !== undefined && next !== undefined && prev.backend !== next.backend) {
    return defaultTile;
  }
  return tile;
}

export default function ModelSelect({ value, onChange, models, disabled, anyOption }: Props) {
  const groups = groupModels(models ?? []);
  const grouped = groups.length > 1;
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
      {anyOption !== undefined && <option value="">{anyOption}</option>}
      {groups.map(({ group, models: members }) => {
        const options = members.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label} — {m.speed}
          </option>
        ));
        return grouped && group !== null ? (
          <optgroup key={group} label={group}>
            {options}
          </optgroup>
        ) : (
          <Fragment key={group ?? "ungrouped"}>{options}</Fragment>
        );
      })}
    </select>
  );
}
