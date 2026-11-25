import type { Box } from "./types";

/** Model-estimated coordinates are hints, not verified PDF evidence. */
export function unverifiedModelSource(parser: string) {
  return parser === "openai-pdf" || parser === "model-page-images";
}

export function sourceTargetBox(parser: string, box: Box | null | undefined) {
  return unverifiedModelSource(parser) ? undefined : box || undefined;
}

export interface PdfRect {
  left: number;
  top: number;
  width: number;
  height: number;
}
export interface PdfCenter {
  x: number;
  y: number;
}
const unit = (value: number) => Math.max(0, Math.min(1, value));

export function pdfViewCenter(page: PdfRect, viewport: PdfRect): PdfCenter {
  return {
    x: unit((viewport.left + viewport.width / 2 - page.left) / Math.max(1, page.width)),
    y: unit((viewport.top + viewport.height / 2 - page.top) / Math.max(1, page.height)),
  };
}

export function pdfCenterScroll(
  page: PdfRect,
  viewport: PdfRect,
  scroll: { left: number; top: number },
  center: PdfCenter,
) {
  return {
    left: Math.max(
      0,
      scroll.left +
        page.left -
        viewport.left +
        page.width * center.x -
        viewport.width / 2,
    ),
    top: Math.max(
      0,
      scroll.top + page.top - viewport.top + page.height * center.y - viewport.height / 2,
    ),
  };
}

export function pdfPageDraft(value: string, total: number): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const number = Number(value.trim());
  return Number.isSafeInteger(number) && number >= 1 && number <= total ? number : null;
}
