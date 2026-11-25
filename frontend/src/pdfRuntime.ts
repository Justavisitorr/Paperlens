import {
  getDocument,
  PDFDataRangeTransport,
  PDFWorker,
  type PDFDocumentProxy,
} from "pdfjs-dist";
import workerSource from "pdfjs-dist/build/pdf.worker.min.mjs?raw";
import type { Box, DashboardReport } from "./types";
import { PDF_RANGE_BYTES, readPdfRange } from "./pdfRanges";
import { resourcePath } from "./reportAssets";

// Every resource is included in the standalone HTML. No filesystem fetch or CDN.
const resources = import.meta.glob<string>(
  [
    "../node_modules/pdfjs-dist/cmaps/*.bcmap",
    "../node_modules/pdfjs-dist/standard_fonts/*.pfb",
    "../node_modules/pdfjs-dist/standard_fonts/*.ttf",
    "../node_modules/pdfjs-dist/wasm/{jbig2,openjpeg,qcms_bg}.wasm",
  ],
  { eager: true, query: "?url", import: "default" },
);
const binaries = new Map(
  Object.entries(resources).map(([path, url]) => [path.split("/").pop()!, url]),
);

export function dataBytes(url: string): Uint8Array {
  const comma = url.indexOf(",");
  if (!url.startsWith("data:") || comma < 0 || !url.slice(0, comma).endsWith(";base64")) {
    throw new Error("原始 PDF 或字体数据不是可离线读取的内置文件。");
  }
  const raw = atob(url.slice(comma + 1));
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

class InlineBinaryDataFactory {
  async fetch({ filename }: { kind: string; filename: string }) {
    const url = binaries.get(filename);
    if (!url) throw new Error(`缺少内置 PDF 资源：${filename}`);
    return dataBytes(url);
  }
}

let worker: PDFWorker | undefined;
function initializeWorker() {
  if (worker) return worker;
  // Chromium rejects module blob workers from file:// documents. A data module
  // works in both moved standalone files and the workspace, without a fetch.
  const port = new Worker(
    `data:text/javascript;charset=utf-8,${encodeURIComponent(workerSource)}`,
    { type: "module" },
  );
  worker = PDFWorker.create({ port });
  return worker;
}

// A report may have both the source pane and several figure crops mounted.
// Keep only a document proxy; PDF.js renders pages when a consumer asks for them.
const documents = new Map<
  string,
  {
    document: Promise<PDFDocumentProxy>;
    references: number;
    destroy: () => Promise<void>;
    errors: Set<(reason: Error) => void>;
    error?: Error;
  }
>();
export function sourceDocument(report: Pick<DashboardReport, "source_pdf">): {
  document: Promise<PDFDocumentProxy>;
  release: () => void;
  onError: (listener: (reason: Error) => void) => () => void;
} {
  const source = report.source_pdf?.data_url;
  if (!source) throw new Error("此报告未内置原始 PDF。");
  const remote = resourcePath(source, "pdf");
  if (!remote && !source.startsWith("data:application/pdf;base64,"))
    throw new Error("原始 PDF 资源地址无效。");
  const key = `${source}\n${report.source_pdf?.sha256 || ""}`;
  let entry = documents.get(key);
  if (!entry) {
    const options = {
      worker: initializeWorker(),
      BinaryDataFactory: InlineBinaryDataFactory,
      useWorkerFetch: false,
      cMapPacked: true,
      useSystemFonts: false,
      disableFontFace: false,
    };
    const controller = new AbortController();
    let task: ReturnType<typeof getDocument> | undefined;
    const created = {
      document: Promise.resolve(null as unknown as PDFDocumentProxy),
      references: 0,
      errors: new Set<(reason: Error) => void>(),
      error: undefined as Error | undefined,
      async destroy() {
        controller.abort();
        await task?.destroy();
      },
    };
    const fail = (reason: unknown) => {
      if (controller.signal.aborted) return;
      created.error = reason instanceof Error ? reason : new Error(String(reason));
      if (documents.get(key) === created) documents.delete(key);
      for (const listener of created.errors) listener(created.error);
      void created.destroy().catch(() => undefined);
    };
    created.document = (async () => {
      if (!remote) task = getDocument({ ...options, data: dataBytes(source) });
      else {
        const suppliedLength = report.source_pdf?.byte_size;
        const knownLength =
          Number.isSafeInteger(suppliedLength) && suppliedLength! > 0
            ? suppliedLength
            : undefined;
        const start = await readPdfRange(
          remote,
          0,
          Math.min(knownLength || PDF_RANGE_BYTES, PDF_RANGE_BYTES),
          controller.signal,
          knownLength,
        );
        if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
        if (start.complete) task = getDocument({ ...options, data: start.bytes });
        else {
          // The hosting relay does not forward Content-Length. Explicit ranges
          // need only the validated Content-Range, so PDF.js cannot eager-fetch
          // the whole file merely because that header is missing.
          const range = new (class extends PDFDataRangeTransport {
            requestDataRange(begin: number, end: number) {
              void readPdfRange(remote, begin, end, controller.signal, start.length)
                .then((part) => {
                  if (!controller.signal.aborted)
                    this.onDataRange(
                      begin,
                      part.complete ? part.bytes.slice(begin, end) : part.bytes,
                    );
                })
                .catch(fail);
            }
            abort() {
              controller.abort();
            }
          })(start.length, start.bytes, true);
          task = getDocument({
            ...options,
            range,
            rangeChunkSize: PDF_RANGE_BYTES,
            disableAutoFetch: true,
            disableStream: true,
          });
        }
      }
      return task.promise;
    })().catch((reason) => {
      fail(reason);
      throw reason;
    });
    entry = created;
    documents.set(key, entry);
  }
  entry.references++;
  const retained = entry;
  let released = false;
  return {
    document: entry.document,
    onError(listener) {
      retained.errors.add(listener);
      if (retained.error) listener(retained.error);
      return () => retained.errors.delete(listener);
    },
    release() {
      if (released) return;
      released = true;
      if (--retained.references === 0) {
        if (documents.get(key) === retained) documents.delete(key);
        // The explicit shared PDFWorker is not owned/destroyed by this task.
        void retained.destroy().catch((error) => console.error("PDF cleanup:", error));
      }
    },
  };
}

export function boxStyle(box: Box) {
  return {
    left: `${box.x0 * 100}%`,
    top: `${box.y0 * 100}%`,
    width: `${(box.x1 - box.x0) * 100}%`,
    height: `${(box.y1 - box.y0) * 100}%`,
  };
}

export function overlaps(a: Box, b: Box): boolean {
  return a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
}

export function coveredBy(a: Box, b: Box): boolean {
  const area = Math.max(0, a.x1 - a.x0) * Math.max(0, a.y1 - a.y0);
  const overlap =
    Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) *
    Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
  return area > 0 && overlap / area >= 0.9;
}
