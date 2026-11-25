import type { Block, ReadingItem } from "./types";

/** Explicit object numbers identify captions, even in older heading projections. */
export function captionKind(text: string): "figure" | "table" | null {
  const match = text
    .trim()
    .match(
      /^(Figure|Fig\.?|Table|Tab\.?|图|表)\s*(?:S?\d+(?:[.\-]\d+)*[a-z]?|[IVX]+)(?=$|[\s:：.、(（\u3400-\u9fff])/i,
    );
  if (!match) return null;
  return /^(?:Table|Tab\.?|表)$/i.test(match[1]) ? "table" : "figure";
}

/** Presentation repair only: keep IDs, original slices, text and saved translations. */
export function normalizeCaptionHeading(
  item: ReadingItem,
  text: string,
  blocks: Map<string, Block>,
): ReadingItem {
  if (item.kind !== "heading") return item;
  const kind = captionKind(text || item.label);
  const sourceCaption = item.parts.some(
    (part) => blocks.get(part.block_id)?.kind === "caption",
  );
  if (!kind && !sourceCaption) return item;
  // An unnumbered source caption remains prose; do not invent an object number/type.
  return kind
    ? { ...item, kind, label: text || item.label }
    : { ...item, kind: "paragraph" };
}

export const sectionHeadings = (items: ReadingItem[]) =>
  items.filter((item) => item.kind === "heading");
