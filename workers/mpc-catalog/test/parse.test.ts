import { describe, expect, it } from "vitest";

import {
  ROOT_CONTEXT,
  buildRecord,
  cardType,
  childContext,
  dpiFor,
  isCardShaped,
  isDiscardFolder,
  mergeTags,
  parseFileName,
  parseFolder,
  stripSortPrefix,
  type FolderContext,
} from "../src/parse";
import { driveFolderId, parseSources } from "../src/sources";

describe("parseFileName", () => {
  it.each([
    // [file name, mime, name, ext, tags, lang]
    ["Sol Ring.png", "image/png", "Sol Ring", "png", [], null],
    ["Sol Ring.JPG", "image/jpeg", "Sol Ring", "jpg", [], null],
    ["Sol Ring.jpeg", "image/jpeg", "Sol Ring", "jpeg", [], null],
    ["Regal Behemoth (CMM, Borderless).png", "image/png", "Regal Behemoth", "png", [], null],
    ["Alien (Token) [WHO] {2}.jpg", "image/jpeg", "Alien", "jpg", [], null],
    ["Angel (Token) [RVR] {3} (1).jpg", "image/jpeg", "Angel", "jpg", [], null],
    ["Ajani Steadfast (Emblem).jpg", "image/jpeg", "Ajani Steadfast", "jpg", [], null],
    ["Lightning Bolt [Retro, Extended].png", "image/png", "Lightning Bolt", "png", ["Retro", "Extended"], null],
    ["Lightning Bolt [Retro] [ Full Art ,, nsfw].png", "image/png", "Lightning Bolt", "png", ["Retro", "Full Art", "NSFW"], null],
    ["Bird (Token) [KTK] {1} [Showcase].jpg", "image/jpeg", "Bird", "jpg", ["Showcase"], null],
    ["Éclair   du  Chaos {FR}.png", "image/png", "Éclair du Chaos", "png", [], "FR"],
    ["Blitz {de} [Retro].png", "image/png", "Blitz", "png", ["Retro"], "DE"],
    ["!How to Play.png", "image/png", "How to Play", "png", [], null],
    ["Sol Ring - [Retro].png", "image/png", "Sol Ring", "png", ["Retro"], null],
    ["No Extension", "image/png", "No Extension", "png", [], null],
    ["Wrong extension.png", "image/jpeg", "Wrong extension", "png", [], null],
    // Drive truncated these mid-group.
    ["Ezio Auditore da Firenze (Borderless, Luisa J.png", "image/png", "Ezio Auditore da Firenze", "png", [], null],
    ["Sol Ring [Retro, Exten.png", "image/png", "Sol Ring", "png", [], null],
    ["Sol Ring (Borderless) [Retro] {12.png", "image/png", "Sol Ring", "png", ["Retro"], null],
    // Copy and collector-number suffixes.
    ["Forest - 301.png", "image/png", "Forest", "png", [], null],
    ["Strip Mine - 82b.png", "image/png", "Strip Mine", "png", [], null],
    ["Sol Ring - Copy.png", "image/png", "Sol Ring", "png", [], null],
    ["Sol Ring - Copy (2).png", "image/png", "Sol Ring", "png", [], null],
    ["Forest - 301 - Copy.png", "image/png", "Forest", "png", [], null],
    ["Island [Retro] - 12a.jpg", "image/jpeg", "Island", "jpg", ["Retro"], null],
    // Left alone.
    ["Mishra_s Factory.png", "image/png", "Mishra_s Factory", "png", [], null],
    ["Borrowing 100,000 Arrows.png", "image/png", "Borrowing 100,000 Arrows", "png", [], null],
    ["Forest-301.png", "image/png", "Forest-301", "png", [], null],
    ["Jace - The Mind Sculptor.png", "image/png", "Jace - The Mind Sculptor", "png", [], null],
    ["Copy Enchantment.png", "image/png", "Copy Enchantment", "png", [], null],
  ])("%s", (file, mime, name, ext, tags, lang) => {
    expect(parseFileName(file, mime)).toEqual({ name, ext, tags, lang });
  });

  it.each([
    ["notes.txt", "text/plain"],
    ["Sol Ring.webp", "image/webp"],
    ["Sol Ring.gif", "image/gif"],
    ["Shortcut", "application/vnd.google-apps.shortcut"],
    ["(1).png", "image/png"],
  ])("skips %s (%s)", (file, mime) => {
    expect(parseFileName(file, mime)).toBeNull();
  });
});

describe("parseFolder", () => {
  it.each([
    // [folder name, tags, lang]
    ["Classic", [], null],
    ["!Misc", [], null],
    ["Borderless ", [], null],
    ["2025", [], null],
    ["!Mursu [Retro, Gold Border, Ancient]", ["Retro", "Gold Border", "Ancient"], null],
    ["Archenemy (Complete) [Scryfall Scan]", ["Scryfall Scan"], null],
    ["Rethemed to LotR [Alternate Art,Extended,Nickname,Universes Beyond]", ["Alternate Art", "Extended", "Nickname", "Universes Beyond"], null],
    ["{FR}  Tokens", [], "FR"],
    ["{DE} LotR [Universes Beyond]", ["Universes Beyond"], "DE"],
    ["{EN} Treasure Cruise Format [Showcase]", ["Showcase"], "EN"],
    ["PT - CARD", [], "PT"],
    ["PT CARDS", [], "PT"],
    ["PT_PW", [], "PT"],
    ["JA", [], "JA"],
    ["French", [], "FR"],
    ["Español [Retro]", ["Retro"], "ES"],
    ["!Japanese", [], "JA"],
    // Not languages: lower-case or longer leading words, unknown codes.
    ["ff", [], null],
    ["FF", [], null],
    ["DFC", [], null],
    ["It That Betrays", [], null],
    ["Deutsch Proxies", [], null],
  ])("%s", (folder, tags, lang) => {
    expect(parseFolder(folder)).toEqual({ tags, lang });
  });
});

describe("discard folders", () => {
  it.each([
    ["!Removed", true],
    ["Removed Cards", true],
    ["trash", true],
    ["Deprecated [Retro]", true],
    ["Old", true],
    ["!Old", true],
    ["Old Versions", true],
    ["OLD versions", true],
    ["Old Version", true],
    ["Old Stuff", true],
    ["old cards [Retro]", true],
    ["Old School", false],
    ["Old Frames", false],
    ["Old Versions of Mine", false],
    ["Unused", true],
    ["!Untagged", false],
    ["Oldenburg", false],
    ["Removal Spells", false],
    ["Not Removed", false],
    ["Trashy", false],
  ])("%s → %s", (folder, discard) => {
    expect(isDiscardFolder(folder)).toBe(discard);
  });
});

describe("cardType", () => {
  it.each([
    [[], "CARD"],
    [["Classic", "Green"], "CARD"],
    [["Cardbacks"], "CARDBACK"],
    [["Misc", "Card Back"], "CARDBACK"],
    [["cardback"], "CARDBACK"],
    [["Tokens"], "TOKEN"],
    [["{FR}  Tokens", "Creatures"], "TOKEN"],
    [["Token"], "TOKEN"],
    [["Tokens", "Cardbacks"], "CARDBACK"],
  ] as const)("%j → %s", (path, type) => {
    expect(cardType(path)).toBe(type);
  });
});

describe("isCardShaped", () => {
  it.each([
    // [w, h, card-shaped]
    [0, 0, true], // unknown dimensions are kept
    [0, 1000, true],
    [3288, 4488, true], // 0.733 portrait
    [1500, 2100, true],
    [744, 1040, true], // 0.715 at 280 dpi
    [4488, 3288, true], // landscape: planes, battles
    [2100, 1500, true], // 1.4
    [1000, 1000, false], // square texture
    [2048, 1024, false], // 2:1
    [1024, 2048, false], // 1:2
    [1300, 1400, false], // 0.93
    [512, 700, false], // card-shaped but 188 dpi
    [700, 512, false],
  ])("%i x %i → %s", (w, h, ok) => {
    expect(isCardShaped(w, h)).toBe(ok);
  });
});

describe("dpiFor", () => {
  it.each([
    [0, 0, 0],
    [3288, 4488, 1208],
    [4488, 3288, 1208],
    [822, 1122, 302],
    [1500, 2100, 565],
  ])("%i x %i → %i dpi", (w, h, dpi) => {
    expect(dpiFor(w, h)).toBe(dpi);
  });
});

describe("tags", () => {
  it("strips only leading sort prefixes", () => {
    expect(stripSortPrefix("!!Misc")).toBe("Misc");
    expect(stripSortPrefix("Wow!")).toBe("Wow!");
  });

  it("merges case-insensitively, keeping the first spelling", () => {
    expect(mergeTags(["Retro", "NSFW"], ["retro", "Extended", "NSFW"])).toEqual(["Retro", "NSFW", "Extended"]);
  });

  it("passes folder tags and the nearest language down, ahead of the file's own", () => {
    let ctx: FolderContext = ROOT_CONTEXT;
    for (const name of ["{FR} Cards", "!Mursu [Retro, Gold Border]", "PT - Extra [Extended, retro]"]) {
      ctx = childContext(ctx, name);
    }
    expect(ctx.tags).toEqual(["Retro", "Gold Border", "Extended"]);
    expect(ctx.lang).toBe("PT");
    expect(ctx.path).toEqual(["{FR} Cards", "!Mursu [Retro, Gold Border]", "PT - Extra [Extended, retro]"]);
  });
});

describe("buildRecord", () => {
  const ctx = childContext(childContext(ROOT_CONTEXT, "Tokens [Showcase]"), "{DE} Neu");

  it("builds a full record", () => {
    const record = buildRecord(
      {
        id: "1AbC",
        name: "Elf Warrior (Token) [Retro, Showcase] {3}.png",
        mimeType: "image/png",
        size: "9123456",
        createdTime: "2024-01-02T03:04:05.000Z",
        modifiedTime: "2024-02-02T03:04:05.000Z",
        imageMediaMetadata: { width: 3288, height: 4488 },
      },
      ctx,
    );
    expect(record).toEqual({
      id: "1AbC",
      name: "Elf Warrior",
      ext: "png",
      type: "TOKEN",
      lang: "DE",
      tags: ["Showcase", "Retro"],
      dpi: 1208,
      w: 3288,
      h: 4488,
      size: 9123456,
      created: "2024-01-02T03:04:05.000Z",
      modified: "2024-02-02T03:04:05.000Z",
      folder: "Tokens [Showcase]/{DE} Neu",
    });
  });

  it("defaults language to EN and zeroes missing metadata", () => {
    const record = buildRecord({ id: "x", name: "Sol Ring {FR}.jpg", mimeType: "image/jpeg" }, ROOT_CONTEXT);
    expect(record).toMatchObject({ lang: "FR", dpi: 0, w: 0, h: 0, size: 0, folder: "", type: "CARD" });
    const plain = buildRecord({ id: "y", name: "Sol Ring.jpg", mimeType: "image/jpeg" }, ROOT_CONTEXT);
    expect(plain?.lang).toBe("EN");
  });

  it("skips non-images", () => {
    expect(buildRecord({ id: "z", name: "readme.txt", mimeType: "text/plain" }, ROOT_CONTEXT)).toBeNull();
  });
});

describe("sources", () => {
  it.each([
    ["https://drive.google.com/open?id=1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA", "1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA"],
    ["https://drive.google.com/drive/folders/15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2", "15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2"],
    ["https://drive.google.com/drive/folders/15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2?usp=sharing", "15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2"],
    ["https://drive.google.com/drive/u/0/folders/15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2/", "15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2"],
    [" https://drive.google.com/open?id=1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA ", "1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA"],
    ["", null],
    ["https://example.com/open?id=1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA", null],
    ["https://drive.google.com/file/d/1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA/view", null],
    ["https://drive.google.com/open?id=short", null],
    ["https://discord.gg/abcdef", null],
  ])("%s → %s", (link, id) => {
    expect(driveFolderId(link)).toBe(id);
  });

  it("keeps Drive-linked sources in MPC Fill's order and counts the rest", () => {
    const body = {
      results: {
        "1": { pk: 1, key: "MrTeferi", name: "MrTeferi", externalLink: "" },
        "2": { pk: 2, key: "JohnPrime", name: "JohnPrime", externalLink: "https://drive.google.com/open?id=1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA" },
        "10": { pk: 10, key: "Zukus 2", name: "Zukus 2", externalLink: "https://drive.google.com/drive/folders/15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2" },
        "5": { pk: 5, key: "WarpDandy", externalLink: "https://drive.google.com/open?id=1wI6DgeKQ1YrFIGfhsfYe4w8XzfMjvoCM" },
        "6": { pk: 6, key: "Linky", externalLink: "https://patreon.com/linky" },
      },
    };
    expect(parseSources(body)).toEqual({
      sources: [
        { key: "JohnPrime", name: "JohnPrime", folderId: "1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA" },
        { key: "WarpDandy", name: "WarpDandy", folderId: "1wI6DgeKQ1YrFIGfhsfYe4w8XzfMjvoCM" },
        { key: "Zukus 2", name: "Zukus 2", folderId: "15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2" },
      ],
      withoutLink: 2,
    });
  });

  it("rejects a response without results", () => {
    expect(() => parseSources({ detail: "nope" })).toThrow(/unexpected/);
  });
});
