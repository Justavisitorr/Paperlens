import type { ReadingMark } from "./readingNotes";

export const MARK_SURFACE = "[data-reading-surface]";

/** DOM text offsets are used only in the displayed paragraph, never as PDF offsets. */
export function pickReadingMark(range: Range, surface: HTMLElement): ReadingMark | null {
  if (!surface.contains(range.startContainer) || !surface.contains(range.endContainer))
    return null;
  const raw = range.toString(),
    quote = raw.trim();
  if (!quote || quote.length > 4000) return null;
  const before = document.createRange();
  before.selectNodeContents(surface);
  before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length + raw.length - raw.trimStart().length;
  return {
    surface:
      surface.dataset.readingSurface === "translation" ? "translation" : "original",
    text: surface.textContent || "",
    start,
    end: start + quote.length,
    color: "yellow",
    style: "highlight",
  };
}

export function readingMarkRange(surface: HTMLElement, mark: ReadingMark): Range | null {
  if (
    surface.dataset.readingSurface !== mark.surface ||
    surface.textContent !== mark.text
  )
    return null;
  const walker = document.createTreeWalker(surface, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let offset = 0,
    started = false;
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const end = offset + (node.textContent?.length || 0);
    if (!started && mark.start >= offset && mark.start < end) {
      range.setStart(node, mark.start - offset);
      started = true;
    }
    if (started && mark.end <= end) {
      range.setEnd(node, mark.end - offset);
      return range;
    }
    offset = end;
  }
  return null;
}
