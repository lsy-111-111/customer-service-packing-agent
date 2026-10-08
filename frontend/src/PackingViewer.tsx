/// <reference types="vite/client" />
import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Download, Maximize, Minus, Plus, X } from "lucide-react";
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy } from "pdfjs-dist";
import workerURL from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { ErrorNote, Spinner } from "./ui";
import "./PackingViewer.css";

GlobalWorkerOptions.workerSrc = workerURL;

export type PackingPDFPage = { page: number; box_number?: number; kind?: string; label?: string };
export type PackingPDF = { url: string; name: string; id?: string; pages?: PackingPDFPage[] };
const pageKind: Record<string, string> = { main: "正唛", side: "侧唛", internal: "内部核验标签", serials: "序列号附页", serial: "序列号附页", appendix: "附页" };

export default function PackingViewer({ document, onClose }: { document: PackingPDF; onClose?: () => void }) {
  const [pdf, setPDF] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [pageInput, setPageInput] = useState("1");
  const [zoom, setZoom] = useState(1);
  const [availableWidth, setAvailableWidth] = useState(700);
  const [loading, setLoading] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState("");
  const viewport = useRef<HTMLDivElement>(null);
  const surface = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let current = true;
    setPDF(null); setPage(1); setPageInput("1"); setZoom(1); setError(""); setLoading(true);
    const loadingTask = getDocument({ url: document.url, cMapUrl: "/pdfjs/cmaps/", cMapPacked: true, standardFontDataUrl: "/pdfjs/standard_fonts/", wasmUrl: "/pdfjs/wasm/", iccUrl: "/pdfjs/iccs/" });
    loadingTask.promise.then(value => { if (current) setPDF(value); }).catch(failure => {
      if (current) setError(failure instanceof Error ? `PDF 暂时无法显示：${failure.message}` : "PDF 暂时无法显示，请重试或下载检查。");
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; void loadingTask.destroy(); };
  }, [document.url]);

  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(entries => setAvailableWidth(Math.max(200, entries[0].contentRect.width - 32)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!pdf) return;
    let current = true;
    let cancel: (() => void) | undefined;
    setRendering(true); setError("");
    pdf.getPage(page).then(async pdfPage => {
      if (!current) return;
      const base = pdfPage.getViewport({ scale: 1 });
      const scale = Math.min(availableWidth / base.width, 1.7) * zoom;
      const view = pdfPage.getViewport({ scale });
      const canvas = window.document.createElement("canvas");
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(12000000 / (view.width * view.height)));
      canvas.width = Math.ceil(view.width * pixelRatio);
      canvas.height = Math.ceil(view.height * pixelRatio);
      canvas.style.width = `${Math.ceil(view.width)}px`;
      canvas.style.height = `${Math.ceil(view.height)}px`;
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", `${document.name}，第 ${page} 页，共 ${pdf.numPages} 页`);
      const context = canvas.getContext("2d");
      if (!context) throw new Error("浏览器暂不能绘制 PDF，请下载文件检查。");
      const render = pdfPage.render({ canvasContext: context, canvas, viewport: view, transform: pixelRatio === 1 ? undefined : [pixelRatio, 0, 0, pixelRatio, 0, 0] });
      cancel = () => render.cancel();
      await render.promise;
      if (current) surface.current?.replaceChildren(canvas);
    }).catch(failure => {
      if (current && failure?.name !== "RenderingCancelledException") setError(failure instanceof Error ? failure.message : "这一页暂时无法显示，请重试。");
    }).finally(() => { if (current) setRendering(false); });
    return () => { current = false; cancel?.(); };
  }, [pdf, page, zoom, availableWidth, document.name]);

  function goToPage(value: number) {
    if (!pdf || !Number.isFinite(value)) return;
    const next = Math.max(1, Math.min(pdf.numPages, Math.round(value)));
    setPage(next); setPageInput(String(next));
    if (viewport.current) { viewport.current.scrollTop = 0; viewport.current.scrollLeft = 0; }
  }
  const currentPage = document.pages?.find(item => item.page === page);
  const boxes = [...new Set(document.pages?.flatMap(item => item.box_number ? [item.box_number] : []) || [])];
  function pageLabel(index: number) {
    const info = document.pages?.find(item => item.page === index);
    return info?.label || (info?.box_number ? `第 ${info.box_number} 箱 · ${pageKind[info.kind || ""] || info.kind || "箱唛"}` : `第 ${index} 页`);
  }

  return <section className="packing-pdf-viewer" aria-label="完整 PDF 预览">
    <div className="packing-viewer-heading"><div><h3>完整 PDF 预览</h3><p>{document.name}</p></div><div className="packing-viewer-actions"><a className="button dark" href={document.url} download={document.name}><Download size={16} />下载当前 PDF</a>{onClose && <button className="button" onClick={onClose}><X size={16} />收起预览</button>}</div></div>
    <p className="packing-muted">这里显示下载文件的实际页面。请逐页核对中文、数量、箱号和边框；打印时选择实际大小。</p>
    <ErrorNote error={error} />
    <div className="packing-viewer-controls">
      <div className="packing-page-controls"><button className="button" aria-label="上一页 PDF" disabled={!pdf || page <= 1} onClick={() => goToPage(page - 1)}><ChevronLeft size={18} />上一页</button><form onSubmit={event => { event.preventDefault(); goToPage(Number(pageInput)); }}><label>页码<input aria-label="PDF 页码" type="number" min="1" max={pdf?.numPages || 1} value={pageInput} onChange={event => setPageInput(event.target.value)} onBlur={() => goToPage(Number(pageInput))} /></label><span> / {pdf?.numPages || "—"} 页</span></form><button className="button" aria-label="下一页 PDF" disabled={!pdf || page >= pdf.numPages} onClick={() => goToPage(page + 1)}>下一页<ChevronRight size={18} /></button></div>
      <div className="packing-zoom-controls"><button className="button" aria-label="缩小 PDF" disabled={zoom <= 0.5 || !pdf} onClick={() => setZoom(value => Math.max(0.5, value - 0.25))}><Minus size={16} /></button><span aria-live="polite">{Math.round(zoom * 100)}%</span><button className="button" aria-label="放大 PDF" disabled={zoom >= 3 || !pdf} onClick={() => setZoom(value => Math.min(3, value + 0.25))}><Plus size={16} /></button><button className="button" disabled={!pdf} onClick={() => setZoom(1)}><Maximize size={16} />适合宽度</button></div>
      {boxes.length > 0 && <label className="packing-box-jump">定位箱号<select aria-label="定位 PDF 箱号" value={currentPage?.box_number || ""} onChange={event => { const found = document.pages?.find(item => item.box_number === Number(event.target.value)); if (found) goToPage(found.page); }}><option value="">选择箱号</option>{boxes.map(number => <option key={number} value={number}>第 {number} 箱</option>)}</select></label>}
    </div>
    {pdf && <nav className="packing-page-list" aria-label="PDF 所有页面">{Array.from({ length: pdf.numPages }, (_, index) => <button key={index} className={page === index + 1 ? "active" : ""} aria-current={page === index + 1 ? "page" : undefined} onClick={() => goToPage(index + 1)}><span>{index + 1}</span>{pageLabel(index + 1)}</button>)}</nav>}
    <div className="packing-viewer-position" aria-live="polite">{pdf ? `第 ${page} / ${pdf.numPages} 页 · ${pageLabel(page)}` : "正在打开 PDF"}{(loading || rendering) && <span><Spinner />正在绘制页面</span>}</div>
    <div className="packing-viewer-viewport" ref={viewport} aria-busy={loading || rendering}><div className="packing-viewer-surface" ref={surface} />{loading && <div className="packing-viewer-loading"><Spinner />正在读取 PDF 文件</div>}</div>
  </section>;
}
