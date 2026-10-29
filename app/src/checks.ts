// The verification report in plain words: one verdict, the spots worth a look, and a short
// list of what was checked. The engine's check names stay technical; this is the only place
// that turns them into sentences.
import type { Check, Report, Status } from "./types";

export interface Spot { page: number; x: number; y: number; w: number; h: number }

export interface Finding {
  key: string;
  status: Status;
  title: string;
  body?: string;
  spots: Spot[];
}

export interface Verdict {
  status: Status;
  title: string;
  sub?: string;
  findings: Finding[];
  checked: string[];
}

const n = (x: unknown) => (typeof x === "number" ? x.toLocaleString() : null);
const plural = (k: number, one: string, many = one + "s") => `${k} ${k === 1 ? one : many}`;

function pagesIn(c: Check): number[] {
  const data = (c as Check & { data?: { pages?: number[] } }).data;
  return data?.pages ?? [];
}

function spotsOf(c: Check): Spot[] {
  const data = (c as Check & { data?: { spots?: Spot[] } }).data;
  return data?.spots ?? [];
}

export function pageText(pages: number[]): string {
  if (!pages.length) return "";
  const shown = pages.slice(0, 4).join(", ");
  return `${pages.length === 1 ? "page" : "pages"} ${shown}${pages.length > 4 ? " …" : ""}`;
}

/** What a check that needs attention says to a person. Returns null to leave it out. */
function finding(c: Check): Finding | null {
  const base = { key: c.name, status: c.status, spots: spotsOf(c) };
  switch (c.name) {
    case "tracked_changes":
      return { ...base, title: "The Word file has edits that were never accepted",
        body: "Accept or reject the tracked changes in Word, then replace the file here. Which version is right is your call, so Livery won't guess." };
    case "text_in_pdf":
    case "text_after_conversion":
    case "mapping_text":
      if (c.status === "FAIL") {
        const missing = (c.data?.missing as string[] | undefined) ?? [];
        return { ...base, title: missing.length === 1 ? "A word from your Word file is missing from the PDF"
            : "Some words from your Word file are missing from the PDF",
          body: missing.length ? `Missing: ${missing.slice(0, 8).map((w) => `“${w}”`).join(", ")}${missing.length > 8 ? " …" : ""}`
            : c.summary };
      }
      return { ...base, title: "Some punctuation differs from the Word file", body: c.summary };
    case "text_in_styled_pdf": {
      const places = base.spots.length || ((c.data?.glued as string[] | undefined) ?? []).length;
      const pages = pagesIn(c);
      return { ...base,
        title: places > 1 ? `Text is too wide for its table column in ${places} places` : "A word is too wide for its table column",
        body: `The text runs into the next cell${pages.length ? ` (${pageText(pages)})` : ""}. Ask the assistant to widen those columns or make the table text a little smaller.` };
    }
    case "structure_after_conversion":
      return { ...base, title: "Some headings, tables or lists didn't carry over the same way",
        body: (c.details ?? []).slice(0, 3).join(" · ") };
    case "render":
      return { ...base, title: "The PDF couldn't be made", body: "The design has an error. Undo the last change or ask the assistant to fix it." };
    case "fonts":
      return { ...base, title: "A font isn't embedded properly", body: "The PDF may look different on other computers." };
    case "images":
      return { ...base, title: c.status === "FAIL" ? "Some images are missing from the PDF" : "Images may not match the Word file", body: c.summary };
    case "heading_numbering":
      return { ...base, title: "Some typed heading numbers don't match the new numbering",
        body: (c.details ?? []).slice(0, 2).join(" · ") };
    case "dropped_paragraphs":
      return { ...base, title: "Some removed paragraphs couldn't be matched",
        body: "Word's table of contents lists entries that aren't headings in the document. " + (c.details ?? []).slice(0, 2).join(" · ") };
    case "mapping_rules":
      return { ...base, title: "Your import rules removed more than expected", body: c.summary };
    case "smartart":
      return { ...base, title: "Diagrams were turned into pictures", body: "Their text can't be checked. Look at them in the preview." };
    case "outline_levels":
      return { ...base, title: "Some paragraphs had a hidden outline level in Word", body: "They were kept as normal paragraphs." };
    case "instrumentation":
      return { ...base, title: "Selecting elements in the preview may not work for this document", body: c.summary };
    case "original_pdf":
    case "typst_warnings":
    case "pandoc_warnings":
    case "lorem_twin":
      return null; // about the tooling, not the PDF the person sends
    default:
      return { ...base, title: c.name.replaceAll("_", " "), body: c.summary };
  }
}

/** One reassuring sentence per thing that was verified and passed. */
function checked(report: Report): string[] {
  const by = Object.fromEntries(report.checks.map((c) => [c.name, c as Check & { data?: Record<string, unknown> }]));
  const out: string[] = [];
  const pdf = by["text_in_pdf"];
  if (pdf?.status === "PASS") {
    const t = n(pdf.data?.tokens);
    out.push(t ? `All ${t} words from your Word file are in the PDF.` : "Every word from your Word file is in the PDF.");
  }
  const st = by["structure_after_conversion"];
  if (st?.status === "PASS") {
    const src = (st.data?.source ?? {}) as { headings?: Record<string, number>; tables?: number; list_items?: number };
    const h = Object.values(src.headings ?? {}).reduce((a, b) => a + b, 0);
    const parts = [h && plural(h, "heading"), src.tables && plural(src.tables, "table"), src.list_items && plural(src.list_items, "list item")]
      .filter(Boolean);
    out.push(parts.length ? `${parts.join(", ")} carried over.` : "Headings, tables and lists carried over.");
  }
  const dp = by["dropped_paragraphs"];
  if (dp?.status === "PASS") out.push("Word's table of contents was replaced by a new one built from your headings.");
  if (by["heading_numbering"]?.status === "PASS") out.push("Heading numbers match the ones typed in Word.");
  if (by["images"]?.status === "PASS") out.push("Every image is in the PDF.");
  if (by["fonts"]?.status === "PASS") out.push("Fonts are embedded, so the PDF looks the same everywhere.");
  if (by["text_in_styled_pdf"]?.status === "PASS") out.push("No text runs out of its table cell.");
  if (by["mapping_text"]?.status === "PASS") out.push("Every word removed on import is on record.");
  return out;
}

export function verdict(report: Report): Verdict {
  const findings = report.checks
    .filter((c) => c.status === "WARN" || c.status === "FAIL")
    .map(finding)
    .filter((f): f is Finding => f !== null)
    .sort((a, b) => (a.status === b.status ? 0 : a.status === "FAIL" ? -1 : 1));
  const fails = findings.filter((f) => f.status === "FAIL");
  const status: Status = fails.length ? "FAIL" : findings.length ? "WARN" : "PASS";
  const words = report.checks.find((c) => c.name === "text_in_pdf") as (Check & { data?: { tokens?: number } }) | undefined;
  const title = status === "PASS" ? "Ready to export"
    : status === "WARN" ? `Ready, but look at ${plural(findings.length, "thing")} first`
    : "Can't export yet";
  const sub = status === "PASS"
    ? (words?.data?.tokens ? `All ${n(words.data.tokens)} words are in the PDF.` : "Nothing missing.")
    : undefined;
  return { status, title, sub, findings, checked: checked(report) };
}

/** Short text for a document's status dot. */
export function statusLine(s: Status | null | undefined): string {
  return s === "PASS" ? "Ready to export" : s === "WARN" ? "Ready, with a few things to look at" : s === "FAIL" ? "Can't export yet" : "Working…";
}
