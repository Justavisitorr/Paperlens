import { useEffect, useMemo, useRef, useState } from "react";
import { TextLayer, type RenderTask } from "pdfjs-dist";
import { boxStyle, coveredBy, overlaps, sourceDocument } from "./pdfRuntime";
import type { Box, DashboardReport } from "./types";
import { AssetImage } from "./AssetImage";
import { resourcePath } from "./reportAssets";
import { downloadFile } from "./export";
import {
  pdfCenterScroll,
  pdfPageDraft,
  pdfViewCenter,
  sourceTargetBox,
  unverifiedModelSource,
  type PdfCenter,
} from "./pdfSourceNavigation";
import "./styles/19-pdf-source.css";

/** The original PDF can be read before a structured analysis exists. */
export interface PdfSourceReport {
  source_pdf?: DashboardReport["source_pdf"];
  previews: DashboardReport["previews"];
  document: Pick<
    DashboardReport["document"],
    "source_name" | "sha256" | "parser" | "blocks"
  > & {
    pages: { number: number; label: string }[];
  };
}

export interface PdfSourceProps {
  report: PdfSourceReport;
  page: number;
  box?: Box | null;
  onClose?: () => void;
  following?: boolean;
  onFollowChange?: (following: boolean) => void;
  onNavigate?: (page: number) => void;
  onManualScroll?: () => void;
}

export function PdfSource({
  report,
  page: requestedPage,
  box,
  onClose,
  following,
  onFollowChange,
  onNavigate,
  onManualScroll,
}: PdfSourceProps) {
  const [attached, setAttached] = useState<{ data_url: string; sha256: string } | null>(
    null,
  );
  const [attachmentError, setAttachmentError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [sourceAttempt, setSourceAttempt] = useState(0);
  const [sourceError, setSourceError] = useState("");
  const effectiveReport = useMemo(
    () =>
      attached?.sha256.toLowerCase() === report.document.sha256.toLowerCase()
        ? { ...report, source_pdf: attached }
        : report,
    [report, attached],
  );
  const modelSource = unverifiedModelSource(report.document.parser);
  const [pageNumber, setPageNumber] = useState(requestedPage);
  const [pageDraft, setPageDraft] = useState(String(requestedPage));
  const [pageError, setPageError] = useState("");
  const [zoom, setZoom] = useState<number | null>(null);
  const [width, setWidth] = useState(600);
  const [dimensions, setDimensions] = useState({ width: 600, height: 800, scale: 1 });
  const renderWidth = zoom === null ? width : 0;
  const [status, setStatus] = useState("正在打开原始 PDF…");
  const [error, setError] = useState("");
  const [rendered, setRendered] = useState<{
    report: PdfSourceReport;
    page: number;
    zoom: number | null;
    width: number;
  } | null>(null);
  const ready =
    rendered?.report === effectiveReport &&
    rendered.page === pageNumber &&
    rendered.zoom === zoom &&
    rendered.width === renderWidth;
  const scroller = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const pageSurface = useRef<HTMLDivElement>(null);
  const pendingCenter = useRef<PdfCenter | null>(null);
  const locatedTarget = useRef<string | null>(null);
  const text = useRef<HTMLDivElement>(null);
  const documentSource = useRef<ReturnType<typeof sourceDocument> | undefined>(undefined);
  const previousRender = useRef<RenderTask | undefined>(undefined);
  const total = report.document.pages.length;
  const metadata = report.document.pages.find((p) => p.number === pageNumber);
  const targetBox =
    pageNumber === requestedPage
      ? sourceTargetBox(report.document.parser, box)
      : undefined;
  const ocrBlocks = report.document.blocks.filter(
    (block) => !modelSource && block.page === pageNumber && block.text_source === "ocr",
  );
  const ocrLines = ocrBlocks.flatMap((block) => {
    let cursor = 0;
    return (
      block.ocr_lines?.length ? block.ocr_lines : [{ text: block.text, bbox: block.bbox }]
    ).map((line) => {
      const start = block.text.indexOf(line.text, cursor);
      if (start >= 0) cursor = start + line.text.length;
      return {
        ...line,
        blockId: start >= 0 ? block.id : undefined,
        start: start >= 0 ? start : undefined,
      };
    });
  });

  useEffect(() => setPageNumber(requestedPage), [requestedPage]);
  useEffect(() => {
    setPageDraft(String(pageNumber));
    setPageError("");
  }, [pageNumber]);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      const style = getComputedStyle(el);
      const available =
        el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      setWidth(Math.max(1, Math.floor(available)));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Keep the PDF proxy alive across page/zoom changes; only the report owns it.
  useEffect(() => {
    locatedTarget.current = null;
    pendingCenter.current = null;
    try {
      const source = sourceDocument(effectiveReport);
      documentSource.current = source;
      setSourceError("");
      const unsubscribe = source.onError((reason) => setSourceError(reason.message));
      return () => {
        unsubscribe();
        documentSource.current = undefined;
        source.release();
      };
    } catch (reason) {
      documentSource.current = undefined;
      setSourceError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [effectiveReport, sourceAttempt]);

  useEffect(() => {
    let cancelled = false;
    let render: RenderTask | undefined;
    let layer: TextLayer | undefined;
    setRendered(null);
    setError("");
    setStatus(`正在读取第 ${pageNumber} 页…`);
    const container = text.current;
    container?.replaceChildren();
    async function draw() {
      try {
        const source = documentSource.current;
        if (sourceError) throw new Error(sourceError);
        if (!source) throw new Error(sourceError || "此报告未内置原始 PDF。");
        const pdf = await source.document;
        const sourcePage = await pdf.getPage(pageNumber);
        if (cancelled || !canvas.current || !container) return;
        const base = sourcePage.getViewport({ scale: 1 });
        const scale = zoom ?? renderWidth / base.width;
        const viewport = sourcePage.getViewport({ scale });
        // PDF.js owns a canvas until its cancelled render has settled.
        await previousRender.current?.promise.catch(() => undefined);
        if (cancelled) return;
        setDimensions({ width: viewport.width, height: viewport.height, scale });
        const output = Math.min(window.devicePixelRatio || 1, 2);
        const surface = canvas.current;
        surface.width = Math.ceil(viewport.width * output);
        surface.height = Math.ceil(viewport.height * output);
        render = sourcePage.render({
          canvas: surface,
          viewport,
          transform: output === 1 ? undefined : [output, 0, 0, output, 0, 0],
        });
        previousRender.current = render;
        await render.promise;
        if (cancelled) return;
        const content = await sourcePage.getTextContent();
        if (cancelled) return;
        container.style.setProperty("--total-scale-factor", String(scale));
        layer = new TextLayer({ textContentSource: content, container, viewport });
        await layer.render();
        if (cancelled) return;
        // OCR replaces only its source region. It must not duplicate bad native
        // text in copy/selection, while valid native text elsewhere remains.
        const rect = container.getBoundingClientRect();
        for (const span of layer.textDivs) {
          const r = span.getBoundingClientRect();
          const nativeBox = {
            x0: (r.left - rect.left) / rect.width,
            y0: (r.top - rect.top) / rect.height,
            x1: (r.right - rect.left) / rect.width,
            y1: (r.bottom - rect.top) / rect.height,
          };
          // Touching a region is insufficient: it can erase a whole healthy
          // line beside the bad text. Only suppress substantially covered spans.
          if (ocrBlocks.some((block) => coveredBy(nativeBox, block.bbox))) {
            span.remove();
            continue;
          }
          if (modelSource) continue;
          const raw = span.textContent || "";
          if (!raw.trim()) continue;
          const matches = report.document.blocks.filter(
            (block) =>
              block.page === pageNumber &&
              block.text_source !== "ocr" &&
              overlaps(nativeBox, block.bbox) &&
              block.text.includes(raw) &&
              block.text.indexOf(raw) === block.text.lastIndexOf(raw),
          );
          if (matches.length === 1) {
            span.dataset.rawText = raw;
            span.dataset.blockId = matches[0].id;
            span.dataset.sourceStart = String(matches[0].text.indexOf(raw));
          }
        }
        setRendered({
          report: effectiveReport,
          page: pageNumber,
          zoom,
          width: renderWidth,
        });
        setStatus("");
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : String(reason));
          setStatus("");
        }
      }
    }
    void draw();
    return () => {
      cancelled = true;
      render?.cancel();
      layer?.cancel();
    };
  }, [
    effectiveReport,
    pageNumber,
    zoom,
    renderWidth,
    sourceAttempt,
    sourceError,
    modelSource,
  ]);

  // A target change is navigation. A render/zoom of that same target is not:
  // retain the point at the user's viewport center rather than jumping back.
  useEffect(() => {
    const element = scroller.current,
      surface = pageSurface.current;
    if (!ready || !element || !surface) return;
    const targetKey = JSON.stringify([
      pageNumber,
      targetBox?.x0,
      targetBox?.y0,
      targetBox?.x1,
      targetBox?.y1,
    ]);
    const newTarget = locatedTarget.current !== targetKey;
    const center = newTarget
      ? targetBox
        ? { x: (targetBox.x0 + targetBox.x1) / 2, y: (targetBox.y0 + targetBox.y1) / 2 }
        : null
      : pendingCenter.current;
    if (center) {
      const rect = element.getBoundingClientRect();
      const scroll = pdfCenterScroll(
        surface.getBoundingClientRect(),
        {
          left: rect.left,
          top: rect.top,
          width: element.clientWidth,
          height: element.clientHeight,
        },
        { left: element.scrollLeft, top: element.scrollTop },
        center,
      );
      element.scrollLeft = scroll.left;
      element.scrollTop = scroll.top;
    } else if (newTarget) {
      element.scrollLeft = 0;
      element.scrollTop = 0;
    }
    locatedTarget.current = targetKey;
    pendingCenter.current = null;
  }, [
    ready,
    pageNumber,
    dimensions.width,
    dimensions.height,
    targetBox?.x0,
    targetBox?.y0,
    targetBox?.x1,
    targetBox?.y1,
  ]);
  const navigate = (page: number) => {
    pendingCenter.current = null;
    setPageDraft(String(page));
    setPageError("");
    setPageNumber(page);
    onNavigate?.(page);
  };
  const commitPage = () => {
    const page = pdfPageDraft(pageDraft, total);
    if (page === null) {
      setPageError(`请输入 1 到 ${total} 之间的整数页码。`);
      return;
    }
    setPageDraft(String(page));
    setPageError("");
    if (page !== pageNumber) navigate(page);
  };
  const changeZoom = (value: number | null) => {
    const element = scroller.current,
      surface = pageSurface.current;
    if (ready && element && surface) {
      const rect = element.getBoundingClientRect();
      pendingCenter.current = pdfViewCenter(surface.getBoundingClientRect(), {
        left: rect.left,
        top: rect.top,
        width: element.clientWidth,
        height: element.clientHeight,
      });
    }
    setZoom(value);
  };

  const effectiveZoom = zoom ?? dimensions.scale;
  const preview = report.previews.find((p) => p.page === pageNumber);
  const attachPdf = async (file: File | undefined) => {
    if (!file) return;
    setAttachmentError("");
    try {
      if (!crypto.subtle || !/^[0-9a-f]{64}$/i.test(report.document.sha256))
        throw new Error("当前环境无法核验原始 PDF，请使用本机服务或离线报告。");
      const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      const sha256 = [...new Uint8Array(digest)]
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
      if (sha256.toLowerCase() !== report.document.sha256.toLowerCase())
        throw new Error("所选文件与本报告的原始 PDF 不一致，请选择对应文件。");
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () =>
          typeof reader.result === "string" ? resolve(reader.result) : reject();
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      setAttached({ data_url: dataUrl, sha256 });
    } catch (reason) {
      setAttachmentError(reason instanceof Error ? reason.message : "无法读取所选 PDF。");
    }
  };
  return (
    <aside
      className="pdf-source"
      aria-label="原始 PDF 核对"
      onPointerDown={(event) => {
        if (!(event.target as Element).closest("button,input,select,textarea,a"))
          scroller.current?.focus({ preventScroll: true });
      }}
    >
      <header className="pdf-source-header">
        <div>
          <strong>原始 PDF</strong>
          <small>{report.document.source_name}</small>
        </div>
        <div className="pdf-source-header-actions">
          {onFollowChange && (
            <button
              type="button"
              className="pdf-follow"
              aria-pressed={!!following}
              onClick={() => onFollowChange(!following)}
            >
              {following ? "跟随正文" : "恢复跟随"}
            </button>
          )}
          {onClose && (
            <button type="button" aria-label="关闭原始 PDF" onClick={onClose}>
              ×
            </button>
          )}
        </div>
      </header>
      <div className="pdf-source-toolbar">
        <button
          type="button"
          disabled={pageNumber <= 1}
          onClick={() => navigate(pageNumber - 1)}
          aria-label="PDF 上一页"
        >
          ‹
        </button>
        <label>
          第{" "}
          <input
            aria-label="PDF 物理页码"
            type="text"
            inputMode="numeric"
            value={pageDraft}
            aria-invalid={!!pageError}
            onChange={(event) => {
              setPageDraft(event.target.value);
              setPageError("");
            }}
            onBlur={commitPage}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                commitPage();
              }
              if (
                event.key === "Escape" &&
                (pageDraft !== String(pageNumber) || pageError)
              ) {
                event.preventDefault();
                event.stopPropagation();
                setPageDraft(String(pageNumber));
                setPageError("");
              }
            }}
          />{" "}
          / {total} 页
        </label>
        <button
          type="button"
          disabled={pageNumber >= total}
          onClick={() => navigate(pageNumber + 1)}
          aria-label="PDF 下一页"
        >
          ›
        </button>
        <span className="pdf-source-toolbar-gap" />
        <button
          type="button"
          onClick={() => changeZoom(Math.max(0.25, effectiveZoom / 1.2))}
          aria-label="缩小 PDF"
        >
          −
        </button>
        <span>{Math.round(effectiveZoom * 100)}%</span>
        <button
          type="button"
          onClick={() => changeZoom(Math.min(4, effectiveZoom * 1.2))}
          aria-label="放大 PDF"
        >
          +
        </button>
        <button type="button" onClick={() => changeZoom(null)} aria-label="PDF 适合宽度">
          适宽
        </button>
      </div>
      {pageError && (
        <p className="pdf-source-label" role="alert">
          {pageError}
        </p>
      )}
      <div className="pdf-source-label">
        物理第 {pageNumber} 页{metadata?.label ? ` · 印刷页码 ${metadata.label}` : ""}
        {ocrLines.length > 0 ? " · OCR 文字层" : " · PDF 文字层"}
        {modelSource && pageNumber === requestedPage && box
          ? " · 仅定位物理页，区域未核实"
          : ""}
      </div>
      <div
        className="pdf-source-scroll"
        ref={scroller}
        onWheel={onManualScroll}
        onTouchMove={onManualScroll}
        onPointerDown={(event) => {
          // A pointer on the scrollbar is manual browsing; selecting text
          // itself remains available without changing the reading position.
          if (event.target === event.currentTarget) onManualScroll?.();
        }}
        tabIndex={0}
        aria-label="原始 PDF 页面"
        onKeyDown={(event) => {
          if (
            ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(
              event.key,
            )
          )
            onManualScroll?.();
        }}
      >
        {status && (
          <p className="pdf-source-status" role="status">
            {status}
          </p>
        )}
        {error && (
          <div className="pdf-source-status" role="alert">
            <p>原始 PDF 暂时无法显示：{error}</p>
            <button
              type="button"
              onClick={() => setSourceAttempt((attempt) => attempt + 1)}
            >
              重试显示
            </button>{" "}
            {effectiveReport.source_pdf?.data_url?.startsWith("data:") ? (
              <a
                href={effectiveReport.source_pdf.data_url}
                download={report.document.source_name}
              >
                下载原始 PDF
              </a>
            ) : (
              resourcePath(effectiveReport.source_pdf?.data_url || "", "pdf") && (
                <button
                  type="button"
                  disabled={downloading}
                  onClick={() => {
                    setDownloading(true);
                    setAttachmentError("");
                    const path = resourcePath(
                      effectiveReport.source_pdf?.data_url || "",
                      "pdf",
                    )!;
                    void fetch(path, { credentials: "same-origin" })
                      .then(async (response) => {
                        if (
                          !response.ok ||
                          !response.headers
                            .get("content-type")
                            ?.startsWith("application/pdf")
                        )
                          throw new Error("原始 PDF 暂时无法下载，请确认连接后重试。");
                        downloadFile(
                          await response.blob(),
                          report.document.source_name,
                          "application/pdf",
                        );
                      })
                      .catch((reason: unknown) =>
                        setAttachmentError(
                          reason instanceof Error ? reason.message : String(reason),
                        ),
                      )
                      .finally(() => setDownloading(false));
                  }}
                >
                  {downloading ? "正在下载…" : "下载原始 PDF"}
                </button>
              )
            )}{" "}
            <label>
              选择原始 PDF
              <input
                type="file"
                accept="application/pdf,.pdf"
                aria-label="选择原始 PDF"
                onChange={(event) => void attachPdf(event.target.files?.[0])}
              />
            </label>
            {attachmentError && <p>{attachmentError}</p>}
          </div>
        )}
        {error && preview && (
          <AssetImage
            className="pdf-source-fallback"
            src={preview.data_url}
            alt={`第 ${pageNumber} 页预览`}
          />
        )}
        <div
          ref={pageSurface}
          className="pdf-source-page"
          data-page={pageNumber}
          data-ready={ready}
          style={{
            width: dimensions.width,
            height: dimensions.height,
            display: error ? "none" : undefined,
          }}
        >
          <canvas ref={canvas} aria-label={`原始 PDF 第 ${pageNumber} 页`} />
          <div ref={text} className="pdf-native-text textLayer" />
          {ready && (
            <div className="pdf-ocr-text">
              {ocrLines.map((line, i) => (
                <span
                  key={i}
                  data-raw-text={line.blockId ? line.text : undefined}
                  data-block-id={line.blockId}
                  data-source-start={line.start}
                  style={{
                    ...boxStyle(line.bbox),
                    fontSize: `${Math.max(8, dimensions.height * (line.bbox.y1 - line.bbox.y0) * 0.8)}px`,
                  }}
                >
                  {line.text}
                </span>
              ))}
            </div>
          )}
          {targetBox && (
            <div
              className="pdf-source-highlight"
              style={boxStyle(targetBox)}
              aria-label="原文定位区域"
            />
          )}
        </div>
      </div>
      {ocrLines.length > 0 && (
        <p className="pdf-source-note">
          OCR 文字可选择复制；识别结果请对照页面图片核实。
        </p>
      )}
    </aside>
  );
}

export function PdfCrop({
  report,
  page,
  box,
  alt,
}: {
  report: DashboardReport;
  page: number;
  box: Box;
  alt: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState(false);
  const [rendered, setRendered] = useState<{
    report: DashboardReport;
    page: number;
    box: string;
  } | null>(null);
  const boxKey = `${box.x0},${box.y0},${box.x1},${box.y1}`;
  const ready =
    rendered?.report === report && rendered.page === page && rendered.box === boxKey;
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "300px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    let render: RenderTask | undefined;
    let source: ReturnType<typeof sourceDocument> | undefined;
    setRendered(null);
    setError(false);
    async function crop() {
      try {
        source = sourceDocument(report);
        const pdf = await source.document;
        const sourcePage = await pdf.getPage(page);
        if (cancelled || !canvas.current) return;
        const base = sourcePage.getViewport({ scale: 1 });
        const regionWidth = base.width * (box.x1 - box.x0);
        const scale = Math.min(3, 1400 / Math.max(1, regionWidth));
        const viewport = sourcePage.getViewport({ scale });
        const target = canvas.current;
        target.width = Math.max(1, Math.round((box.x1 - box.x0) * viewport.width));
        target.height = Math.max(1, Math.round((box.y1 - box.y0) * viewport.height));
        render = sourcePage.render({
          canvas: target,
          viewport,
          transform: [1, 0, 0, 1, -box.x0 * viewport.width, -box.y0 * viewport.height],
        });
        await render.promise;
        if (cancelled) return;
        setRendered({ report, page, box: boxKey });
      } catch {
        if (!cancelled) setError(true);
      }
    }
    void crop();
    return () => {
      cancelled = true;
      render?.cancel();
      source?.release();
    };
  }, [report, page, box.x0, box.y0, box.x1, box.y1, visible]);
  if (error)
    return (
      <span className="pdf-crop-unavailable">
        图像暂时无法显示，请打开原始 PDF 核对。
      </span>
    );
  return (
    <canvas
      className="pdf-crop"
      ref={canvas}
      role="img"
      aria-label={alt}
      data-ready={ready}
    />
  );
}
