// Reading picked/dropped image files in the webview, shared by the Back
// Library and the Custom Image library.
//
// Both libraries take the same three things from a file — the raw bytes,
// its true dimensions, and a small JPEG preview — and both hand them to
// Rust, which owns hashing and storage. Extracting this was not just
// tidying: the size cap and the "is this actually an image" check are the
// only validation standing between a dropped file and the store, and two
// copies of them would eventually disagree about what is allowed.
//
// Decoding happens here rather than in Rust deliberately: generating a
// 220px JPEG natively would mean adding an image-decoding crate to a build
// that ships in six platform variants, and the webview already has to
// decode the file to show the user what they picked.

const THUMB_MAX_PX = 220;

// Mirrors proxy_scaler/backs.py and customs.py (MAX_UPLOAD_BYTES) and the
// Rust MAX_BYTES. Enforced in all three on purpose: this one fails fast in
// front of the user, the others protect their own store.
export const MAX_UPLOAD_MB = 50;

/** What the file pickers accept, and what a drop is checked against. */
export const ACCEPTED_IMAGE_TYPES = "image/png,image/jpeg,image/webp";

export interface PickedImage {
  bytes: number[];
  thumbnail: number[];
  width: number;
  height: number;
}

/**
 * Decode one picked file, measure it, and render the small preview the
 * Rust side stores alongside it.
 *
 * Throws with a user-facing message — every caller surfaces it verbatim.
 */
export async function readPickedImage(file: File): Promise<PickedImage> {
  // Drops bypass the picker's `accept`, so the type check has to happen
  // here rather than being left to the input element.
  if (!file.type.startsWith("image/")) {
    throw new Error(`${file.name} isn't an image.`);
  }
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
    throw new Error(`${file.name} is larger than the ${MAX_UPLOAD_MB}MB limit.`);
  }

  const buffer = await file.arrayBuffer();
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(new Blob([buffer], { type: file.type }));
  } catch {
    // A file that claims an image type but isn't one (or is a format this
    // webview can't decode) — say so in the user's terms rather than
    // letting a decoder exception through.
    throw new Error(`${file.name} couldn't be read as an image.`);
  }
  const scale = Math.min(1, THUMB_MAX_PX / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Couldn't render a preview for that image.");
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", 0.85),
  );
  if (!blob) throw new Error("Couldn't render a preview for that image.");
  const thumbBuffer = await blob.arrayBuffer();
  return {
    bytes: Array.from(new Uint8Array(buffer)),
    thumbnail: Array.from(new Uint8Array(thumbBuffer)),
    width: bitmap.width,
    height: bitmap.height,
  };
}

export interface BatchResult<T> {
  added: T[];
  errors: string[];
}

/**
 * Read and add several files one at a time, collecting failures instead of
 * aborting.
 *
 * Sequential on purpose. Decoding a file to a bitmap and re-encoding a
 * thumbnail holds the whole image in memory, and a bulk drop is exactly
 * when someone selects thirty 40MB scans at once — doing those in parallel
 * is how the webview runs out of memory partway through and loses the ones
 * that had already succeeded. `onProgress` exists so the UI can say which
 * file it is on, since sequential means a big drop takes visible time.
 */
export async function addImagesSequentially<T>(
  files: File[],
  add: (file: File, picked: PickedImage) => Promise<T>,
  onProgress?: (done: number, total: number) => void,
): Promise<BatchResult<T>> {
  const added: T[] = [];
  const errors: string[] = [];
  for (const [index, file] of files.entries()) {
    onProgress?.(index, files.length);
    try {
      added.push(await add(file, await readPickedImage(file)));
    } catch (err) {
      errors.push(err instanceof Error ? err.message : String(err));
    }
  }
  onProgress?.(files.length, files.length);
  return { added, errors };
}
