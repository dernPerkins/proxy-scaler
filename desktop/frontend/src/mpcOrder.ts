// Reading an MPC Autofill order file (the XML MPC Fill's "Download XML"
// button produces) into cards for a project.
//
// The format, as MPC Fill writes it:
//
//   <order>
//     <details>…quantity, bracket, stock, foil…</details>
//     <fronts><card><id>DRIVE_ID</id><slots>0,1,2</slots><name>…</name>
//             <query>…</query></card>…</fronts>
//     <backs>…the same, for slots with their own back…</backs>
//     <cardback>DRIVE_ID</cardback>
//   </order>
//
// Every slot is one physical card. A slot's front comes from <fronts>; its
// back is its <backs> entry when it has one, else the order's <cardback>.
// <details> and <query> aren't needed here. Pure functions only.

const DRIVE_ID = /^[A-Za-z0-9_-]{20,80}$/;

export interface OrderArt {
  driveId: string;
  /** MPC Fill's name for the file, extension and all. */
  fileName: string;
}

/** One project row the order asks for: a front, its copies, and the
 *  slot-specific back those copies print with (null: the project's). */
export interface OrderLine {
  front: OrderArt;
  back: OrderArt | null;
  quantity: number;
}

export interface ParsedOrder {
  lines: OrderLine[];
  /** The order's default back, if it names one. */
  cardback: OrderArt | null;
  /** Every distinct front and back to fetch, fronts first. */
  fronts: OrderArt[];
  backs: OrderArt[];
  /** Problems worth telling the user about (entries skipped). */
  warnings: string[];
}

function text(el: Element, tag: string): string {
  return el.getElementsByTagName(tag)[0]?.textContent?.trim() ?? "";
}

function readCards(
  parent: Element | undefined,
  warnings: string[],
  side: string,
): { art: OrderArt; slots: number[] }[] {
  if (!parent) return [];
  const out: { art: OrderArt; slots: number[] }[] = [];
  for (const card of Array.from(parent.getElementsByTagName("card"))) {
    const driveId = text(card, "id");
    const fileName = text(card, "name");
    if (!DRIVE_ID.test(driveId)) {
      warnings.push(`Skipped a ${side} with an unreadable id${fileName ? ` (${fileName})` : ""}.`);
      continue;
    }
    const slots = text(card, "slots")
      .split(",")
      .map((s) => Number.parseInt(s.trim(), 10))
      .filter((n) => Number.isInteger(n) && n >= 0);
    if (slots.length === 0) {
      warnings.push(`Skipped ${fileName || driveId}: it has no slots.`);
      continue;
    }
    out.push({ art: { driveId, fileName }, slots });
  }
  return out;
}

/** Parse an order file's text. Throws with a user-facing message when it
 *  isn't an MPC Autofill order at all. */
export function parseMpcOrder(xml: string): ParsedOrder {
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  if (doc.getElementsByTagName("parsererror").length > 0) {
    throw new Error("That file isn't valid XML.");
  }
  const order = doc.documentElement;
  if (!order || order.tagName !== "order") {
    throw new Error("That file isn't an MPC Autofill order (no <order>).");
  }
  const warnings: string[] = [];
  const fronts = readCards(order.getElementsByTagName("fronts")[0], warnings, "front");
  const backs = readCards(order.getElementsByTagName("backs")[0], warnings, "back");
  const cardbackId = text(order, "cardback");
  const cardback = DRIVE_ID.test(cardbackId) ? { driveId: cardbackId, fileName: "" } : null;

  const backBySlot = new Map<number, OrderArt>();
  for (const { art, slots } of backs) for (const slot of slots) backBySlot.set(slot, art);

  // Count copies per (front, back), first-seen order.
  const lines = new Map<string, OrderLine>();
  for (const { art, slots } of fronts) {
    for (const slot of slots) {
      const own = backBySlot.get(slot);
      const back = own && own.driveId !== cardback?.driveId ? own : null;
      const key = `${art.driveId}|${back?.driveId ?? ""}`;
      const line = lines.get(key);
      if (line) line.quantity += 1;
      else lines.set(key, { front: art, back, quantity: 1 });
    }
  }
  if (lines.size === 0) throw new Error("That order has no cards in it.");

  const distinct = (arts: OrderArt[]) => [
    ...new Map(arts.map((a) => [a.driveId, a])).values(),
  ];
  const lineList = [...lines.values()];
  return {
    lines: lineList,
    cardback,
    fronts: distinct(lineList.map((l) => l.front)),
    backs: distinct([
      ...lineList.flatMap((l) => (l.back ? [l.back] : [])),
      ...(cardback ? [cardback] : []),
    ]),
    warnings,
  };
}

/** MPC Fill's file name without its extension, for a card's name. */
export function orderArtName(art: OrderArt): string {
  return art.fileName.replace(/\.(png|jpe?g|webp)$/i, "").trim();
}
