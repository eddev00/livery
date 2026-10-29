import { useEffect, useMemo, useRef, useState } from "react";
import { openPdf, type PdfDoc } from "../pdf";
import type { Spot } from "../checks";
import type { MapElement, SelectionMap } from "../types";

interface Props {
  data: Uint8Array | null;
  map: SelectionMap | null;
  /** a scale, or "fit" for the page width of the pane */
  zoom: number | "fit";
  selected: Set<string>;
  onSelect?: (el: MapElement, additive: boolean) => void;
  onUnsupported?: (el: MapElement) => void;
  emptyText: string;
  /** a spot to scroll into view and flash (from the report's "Show me") */
  focus?: { spot: Spot; seq: number } | null;
}

/** One PDF with the selection overlay: every mapped element is a set of rectangles (one per
 *  page it touches) sharing its ID; hovering shows the label, clicking selects it. */
export function PdfView({ data, map, zoom: zoomProp, selected, onSelect, onUnsupported, emptyText, focus }: Props) {
  const [doc, setDoc] = useState<PdfDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const holder = useRef<HTMLDivElement>(null);
  const [paneWidth, setPaneWidth] = useState(800);
  const [pageWidth, setPageWidth] = useState(595);

  useEffect(() => {
    // measure the scrolling pane, not the page column (whose width follows the zoom)
    const node = holder.current?.parentElement;
    if (!node) return;
    setPaneWidth(node.clientWidth);
    const ro = new ResizeObserver(() => setPaneWidth(node.clientWidth));
    ro.observe(node);
    return () => ro.disconnect();
  }, [doc]);

  useEffect(() => {
    if (doc) void doc.getPage(1).then((p) => setPageWidth(p.getViewport({ scale: 1 }).width));
  }, [doc]);

  const zoom = zoomProp === "fit" ? Math.max(0.3, Math.min(2.5, (paneWidth - 40) / pageWidth)) : zoomProp;

  // Scroll the spot to the middle of the pane; the highlight fades out on its own (CSS).
  useEffect(() => {
    if (!focus || !doc) return;
    const pane = holder.current?.parentElement;
    const page = holder.current?.children[focus.spot.page - 1] as HTMLElement | undefined;
    if (!pane || !page) return;
    const pr = page.getBoundingClientRect(), vr = pane.getBoundingClientRect();
    const y = pane.scrollTop + pr.top - vr.top + (focus.spot.y + focus.spot.h / 2) * zoom - pane.clientHeight / 2;
    const x = pane.scrollLeft + pr.left - vr.left + (focus.spot.x + focus.spot.w / 2) * zoom - pane.clientWidth / 2;
    // glide to nearby spots, jump straight to far ones
    const far = Math.abs(y - pane.scrollTop) > pane.clientHeight * 3;
    pane.scrollTo({ top: Math.max(0, y), left: Math.max(0, x), behavior: far ? "auto" : "smooth" });
  }, [focus?.seq, doc]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let cancelled = false;
    setDoc(null);
    setError(null);
    if (!data) return;
    // pdf.js takes ownership of the buffer: give it a copy
    openPdf(data.slice())
      .then((d) => !cancelled && setDoc(d))
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [data]);

  const byPage = useMemo(() => {
    const out = new Map<number, { el: MapElement; i: number }[]>();
    for (const el of map?.elements ?? []) {
      el.rects.forEach((r, i) => {
        if (!out.has(r.page)) out.set(r.page, []);
        out.get(r.page)!.push({ el, i });
      });
    }
    return out;
  }, [map]);

  if (error) return <div className="pdf-empty">Could not open the PDF: {error}</div>;
  if (!data) return <div className="pdf-empty">{emptyText}</div>;
  if (!doc) return <div className="pdf-empty">Loading…</div>;

  return (
    <div className="pdf-pages" ref={holder}>
      {Array.from({ length: doc.numPages }, (_, i) => (
        <PdfPage key={i} doc={doc} page={i + 1} zoom={zoom}>
          {(byPage.get(i + 1) ?? []).map(({ el, i: ri }) => {
            const r = el.rects[ri];
            const cls = ["el", `el-${el.kind}`, el.selectable ? "" : "el-unsupported",
              selected.has(el.id) ? "el-selected" : "", hover === el.id ? "el-hover" : ""].join(" ");
            return (
              <div
                key={`${el.id}-${ri}`}
                className={cls}
                style={{ left: r.x * zoom, top: r.y * zoom, width: r.w * zoom, height: r.h * zoom }}
                title={el.selectable ? el.label : `${el.label} — not supported for selection yet`}
                onMouseEnter={() => setHover(el.id)}
                onMouseLeave={() => setHover((h) => (h === el.id ? null : h))}
                onClick={(e) => {
                  e.stopPropagation();
                  if (!el.selectable) onUnsupported?.(el);
                  else onSelect?.(el, e.ctrlKey || e.metaKey || e.shiftKey);
                }}
              >
                {(hover === el.id || selected.has(el.id)) && ri === 0 && <span className="el-label">{el.label}</span>}
              </div>
            );
          })}
          {focus && focus.spot.page === i + 1 && (
            <div key={focus.seq} className="spot-focus" aria-hidden="true"
              style={{ left: (focus.spot.x - 4) * zoom, top: (focus.spot.y - 3) * zoom,
                width: (focus.spot.w + 8) * zoom, height: (focus.spot.h + 6) * zoom }} />
          )}
        </PdfPage>
      ))}
    </div>
  );
}

function PdfPage({ doc, page, zoom, children }: { doc: PdfDoc; page: number; zoom: number; children: React.ReactNode }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const [visible, setVisible] = useState(page <= 2);

  useEffect(() => {
    let cancelled = false;
    void doc.getPage(page).then((p) => {
      if (cancelled) return;
      const vp = p.getViewport({ scale: 1 });
      setSize({ w: vp.width, h: vp.height });
    });
    return () => {
      cancelled = true;
    };
  }, [doc, page]);

  useEffect(() => {
    const node = box.current;
    if (!node) return;
    const io = new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && setVisible(true), { rootMargin: "800px" });
    io.observe(node);
    return () => io.disconnect();
  }, [size]);

  useEffect(() => {
    if (!visible || !size || !canvas.current) return;
    let task: { cancel: () => void; promise: Promise<void> } | null = null;
    let cancelled = false;
    void doc.getPage(page).then((p) => {
      if (cancelled || !canvas.current) return;
      const dpr = window.devicePixelRatio || 1;
      const vp = p.getViewport({ scale: zoom * dpr });
      const c = canvas.current;
      c.width = Math.floor(vp.width);
      c.height = Math.floor(vp.height);
      task = p.render({ canvas: c, viewport: vp });
      task.promise.catch(() => undefined);
    });
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [doc, page, zoom, visible, size]);

  const w = (size?.w ?? 595) * zoom;
  const h = (size?.h ?? 842) * zoom;
  return (
    <div className="pdf-page" ref={box} style={{ width: w, height: h }}>
      <canvas ref={canvas} style={{ width: w, height: h }} />
      <div className="pdf-overlay">{children}</div>
      <div className="pdf-page-number">{page}</div>
    </div>
  );
}
