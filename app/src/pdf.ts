// pdf.js in the sandboxed page: the worker code runs on the main thread (module workers
// from file:// are not reliable) and nothing is fetched. The page's CSP forbids eval.
import * as pdfjs from "pdfjs-dist";
import * as worker from "pdfjs-dist/build/pdf.worker.min.mjs";

(globalThis as unknown as { pdfjsWorker: unknown }).pdfjsWorker = worker;

export type PdfDoc = pdfjs.PDFDocumentProxy;

export async function openPdf(data: Uint8Array): Promise<PdfDoc> {
  return pdfjs.getDocument({ data, useSystemFonts: false, disableAutoFetch: true }).promise;
}
