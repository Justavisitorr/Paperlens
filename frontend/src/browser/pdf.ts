import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";
import { sourceDocument } from "../pdfRuntime";
import type { Block, Box, DashboardReport } from "../types";
import type { PageImage } from "./provider";

type Source = NonNullable<DashboardReport["source_pdf"]>;
export type PageMeta = DashboardReport["document"]["pages"][number];
export interface LocalPage {
  meta: PageMeta;
  blocks: Block[];
}
export interface LocalPdf {
  source: Source;
  pdf: PDFDocumentProxy;
  labels: string[];
  filename: string;
  release: () => void;
}
export function asDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("无法读取所选文件。"));
    reader.readAsDataURL(blob);
  });
}
export async function loadPdf(file: File): Promise<LocalPdf> {
  const [url, bytes] = await Promise.all([
    asDataUrl(file.slice(0, file.size, "application/pdf")),
    file.arrayBuffer(),
  ]);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  const source = {
    data_url: url,
    sha256: Array.from(new Uint8Array(hash), (char) =>
      char.toString(16).padStart(2, "0"),
    ).join(""),
    byte_size: file.size,
  };
  const handle = sourceDocument({ source_pdf: source });
  try {
    const pdf = await handle.document;
    const labels = await pdf.getPageLabels();
    return {
      source,
      pdf,
      labels: labels || [],
      filename: file.name,
      release: handle.release,
    };
  } catch (error) {
    handle.release();
    if (error instanceof Error && error.name === "PasswordException")
      throw new Error("这份 PDF 受密码保护，请选择已解锁的副本。");
    throw new Error("无法打开这份 PDF，请确认文件完整且可以正常阅读。");
  }
}

const within = (n: number) => Math.max(0, Math.min(1, n));
export function union(a: Box, b: Box): Box {
  return {
    x0: Math.min(a.x0, b.x0),
    y0: Math.min(a.y0, b.y0),
    x1: Math.max(a.x1, b.x1),
    y1: Math.max(a.y1, b.y1),
  };
}
async function pageText(page: PDFPageProxy, number: number): Promise<Block[]> {
  const viewport = page.getViewport({ scale: 1 });
  const text = await page.getTextContent();
  const lines: { text: string; box: Box }[] = [];
  let line: { text: string; box: Box } | undefined;
  function flush() {
    if (line?.text.trim()) lines.push(line);
    line = undefined;
  }
  for (const item of text.items) {
    if (!("str" in item)) continue;
    if (item.str.trim()) {
      const [a, b, c, d, x, y] = item.transform;
      const h = Math.hypot(c, d) || Math.abs(item.height) || 1;
      const w = Math.hypot(a, b) || 1;
      const endX = x + (a / w) * item.width,
        endY = y + (b / w) * item.width;
      const points = [
        [x, y],
        [endX, endY],
        [x + (c / h) * item.height, y + (d / h) * item.height],
        [endX + (c / h) * item.height, endY + (d / h) * item.height],
      ].map(([px, py]) => viewport.convertToViewportPoint(px, py));
      const xs = points.map((p) => within(p[0] / viewport.width)),
        ys = points.map((p) => within(p[1] / viewport.height));
      const box = {
        x0: Math.min(...xs),
        x1: Math.max(...xs),
        y0: Math.min(...ys),
        y1: Math.max(...ys),
      };
      const gap = line && box.x0 - line.box.x1;
      if (line && (Math.abs(line.box.y1 - box.y1) > 0.012 || (gap || 0) > 0.075)) flush();
      if (line) {
        line.text += ((gap || 0) > 0.001 && !/\s$/.test(line.text) ? " " : "") + item.str;
        line.box = union(line.box, box);
      } else line = { text: item.str, box };
    } else if (line && item.str) line.text += " ";
    if (item.hasEOL) flush();
  }
  flush();
  const paragraphs: typeof lines = [];
  for (const ln of lines) {
    const prev = paragraphs.at(-1),
      lastHeight = ln.box.y1 - ln.box.y0;
    const gap = prev ? ln.box.y0 - prev.box.y1 : Infinity;
    if (
      prev &&
      gap >= -0.001 &&
      gap < lastHeight * 0.7 &&
      Math.abs(ln.box.x0 - prev.box.x0) < 0.025 &&
      prev.text.length < 1300
    ) {
      prev.text += " " + ln.text.trim();
      prev.box = union(prev.box, ln.box);
    } else paragraphs.push({ ...ln });
  }
  return paragraphs.map((p, i) => ({
    id: "p" + number + "b" + (i + 1),
    page: number,
    text: p.text.trim(),
    raw_text: p.text.trim(),
    bbox: p.box,
    text_source: "pdf_text",
    kind: "text",
    order: i,
    section: "",
  }));
}
export async function renderForModel(
  doc: LocalPdf,
  number: number,
  signal: AbortSignal,
): Promise<{ page: LocalPage; input: PageImage }> {
  signal.throwIfAborted();
  const source = await doc.pdf.getPage(number);
  const canvas = document.createElement("canvas");
  try {
    const base = source.getViewport({ scale: 1 });
    const view = source.getViewport({ scale: 1650 / Math.max(base.width, base.height) });
    canvas.width = Math.max(1, Math.ceil(view.width));
    canvas.height = Math.max(1, Math.ceil(view.height));
    const blocks = await pageText(source, number).catch(() => [] as Block[]);
    signal.throwIfAborted();
    const task = source.render({ canvas, viewport: view, background: "white" });
    const stop = () => task.cancel();
    signal.addEventListener("abort", stop, { once: true });
    try {
      await task.promise;
    } finally {
      signal.removeEventListener("abort", stop);
    }
    signal.throwIfAborted();
    const image = canvas.toDataURL("image/jpeg", 0.88);
    const meta = {
      number,
      width: base.width,
      height: base.height,
      rotation: base.rotation,
      label: doc.labels[number - 1] || String(number),
      text_chars: blocks.reduce((n, b) => n + b.text.length, 0),
      status: blocks.length ? "ok" : "image_only",
      warnings: [],
    };
    return {
      page: { meta, blocks },
      input: { number, image, hasText: blocks.length > 0 },
    };
  } finally {
    canvas.width = canvas.height = 0;
    source.cleanup();
  }
}
