/** Binary resources are either embedded or served by this paper's authenticated API. */
import type { DashboardReport } from "./types";

export function resourcePath(source: string, kind: "image" | "pdf"): string | null {
  let path = source;
  if (!source.startsWith("/")) {
    try {
      const url = new URL(source);
      if (!/^https?:$/.test(url.protocol) || url.origin !== location.origin) return null;
      path = url.pathname + url.search + url.hash;
    } catch {
      return null;
    }
  }
  const suffix = kind === "pdf" ? "pdf" : "(?:assets/[a-f0-9]{64}|thumbnail)";
  return new RegExp(`^/api/library/[A-Za-z0-9_-]+/${suffix}$`).test(path) ? path : null;
}

export function embeddedImage(source: string): boolean {
  return /^data:image\/(?:png|jpe?g|gif|webp|avif);base64,/i.test(source);
}

export function hasExternalAssets(report: DashboardReport): boolean {
  const reading = report.reading;
  const items = [
    ...(reading?.items || []),
    ...(reading?.references || []),
    ...Object.values(reading?.information_items || {}).flat(),
  ];
  return Boolean(
    (report.source_pdf?.data_url && !report.source_pdf.data_url.startsWith("data:")) ||
    report.previews?.some(
      (preview) => preview.data_url && !embeddedImage(preview.data_url),
    ) ||
    items.some((item) => item.asset && !embeddedImage(item.asset)),
  );
}

type ImageEntry = {
  references: number;
  controller: AbortController;
  promise: Promise<string>;
  objectUrl?: string;
};
const images = new Map<string, ImageEntry>();

/** Share overlapping thumbnail/modal loads, then release their decoded binary storage. */
export function retainImage(source: string): {
  url: Promise<string>;
  release: () => void;
} {
  const path = resourcePath(source, "image");
  if (!path) throw new Error("图片资源地址无效。");
  let entry = images.get(path);
  if (!entry) {
    const controller = new AbortController();
    const created: ImageEntry = {
      references: 0,
      controller,
      promise: Promise.resolve(""),
    };
    created.promise = (async () => {
      // window.fetch is bound to the workspace that opened this tab by the web adapter.
      const response = await fetch(path, {
        signal: controller.signal,
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`图片暂时无法读取（${response.status}）。`);
      const blob = await response.blob();
      if (!/^image\/(?:png|jpe?g|gif|webp|avif)$/i.test(blob.type))
        throw new Error("服务器未返回有效图片。");
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      created.objectUrl = URL.createObjectURL(blob);
      return created.objectUrl;
    })().catch((error) => {
      if (images.get(path) === created) images.delete(path);
      throw error;
    });
    images.set(path, created);
    entry = created;
  }
  const retained = entry;
  retained.references++;
  let released = false;
  return {
    url: retained.promise,
    release() {
      if (released) return;
      released = true;
      if (--retained.references === 0) {
        if (images.get(path) === retained) images.delete(path);
        retained.controller.abort();
        if (retained.objectUrl) URL.revokeObjectURL(retained.objectUrl);
      }
    },
  };
}
