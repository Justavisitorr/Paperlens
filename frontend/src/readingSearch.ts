import { readingDocument, readingText } from "./reading";
import type { DashboardReport } from "./types";

export interface ReadingMatch {
  id: string;
  text: string;
  page: number;
  section: string;
}

/** Both the result list and the reading controls locate the same projected items. */
export function readingMatches(report: DashboardReport, query: string): ReadingMatch[] {
  const search = query.trim().toLocaleLowerCase();
  if (!search) return [];
  const reading = readingDocument(report);
  const blocks = new Map(report.document.blocks.map((block) => [block.id, block]));
  return [...reading.items, ...reading.references].flatMap((item) => {
    const text = readingText(item, blocks);
    if (!text.toLocaleLowerCase().includes(search)) return [];
    return [
      {
        id: item.id,
        text,
        page: item.page,
        section:
          item.kind === "reference"
            ? "参考文献"
            : blocks.get(item.parts[0]?.block_id)?.section || "",
      },
    ];
  });
}

export function stepSearchIndex(
  current: number,
  count: number,
  direction: -1 | 1,
): number {
  if (count <= 0) return -1;
  if (current < 0 || current >= count) return direction === 1 ? 0 : count - 1;
  return (current + direction + count) % count;
}
