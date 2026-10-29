import type { RefImage } from "./types";

export const MAX_IMAGES = 4;
const EDGE = 2000; // the engine's limit too; smaller images travel faster
const TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
export const IMAGE_ACCEPT = TYPES.join(",");
let seq = 0;

/** The image files among dropped or pasted files. */
export function imageFiles(files: FileList | File[] | null | undefined): File[] {
  return Array.from(files ?? []).filter((f) => TYPES.includes(f.type));
}

/** A reference image drawn onto a canvas and saved as PNG: scaled down to EDGE and with
 * no metadata. The engine re-encodes it again; this keeps the message small. */
export async function prepareImage(file: File): Promise<RefImage> {
  const bmp = await createImageBitmap(file);
  const scale = Math.min(1, EDGE / Math.max(bmp.width, bmp.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bmp.width * scale));
  canvas.height = Math.max(1, Math.round(bmp.height * scale));
  canvas.getContext("2d")!.drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close();
  const url = canvas.toDataURL("image/png");
  return { id: `img${++seq}`, url, data: url.slice(url.indexOf(",") + 1) };
}
