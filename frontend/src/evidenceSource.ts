import { pageRanges } from "./qualityWarnings";
import type { Evidence } from "./types";

/** Merged source paragraphs retain possible pages, not an invented quote box. */
export function evidencePages(
  evidence: Pick<Evidence, "page" | "source_pages">,
): number[] {
  return [...new Set([evidence.page, ...(evidence.source_pages || [])])]
    .filter((page) => Number.isInteger(page) && page > 0)
    .sort((a, b) => a - b);
}

export function evidencePageLabel(
  evidence: Pick<Evidence, "page" | "source_pages">,
): string {
  return `PDF ${pageRanges(evidencePages(evidence))}`;
}
