import type { Block, ReadingItem } from "./types";

/** Display consecutive listing pages together; keep every stored item/anchor intact. */
export function readingRuns(items: ReadingItem[], blocks: Map<string, Block>) {
  const runs: ReadingItem[][] = [];
  for (const item of items) {
    const run = runs.at(-1);
    const previous = run?.at(-1);
    let continues = false;
    if (previous?.kind === "code" && item.kind === "code") {
      const first = run![0];
      const before = previous.parts.at(-1);
      const after = item.parts[0];
      const beforeBlock = before && blocks.get(before.block_id);
      const afterBlock = after && blocks.get(after.block_id);
      const endBox = before?.bbox ?? beforeBlock?.bbox ?? previous.bbox;
      const startBox = item.bbox ?? after?.bbox ?? afterBlock?.bbox;
      const endPage = beforeBlock?.page ?? previous.source_pages?.at(-1) ?? previous.page;
      const caption = (text: string) =>
        text.replace(/\s*\(?continued\)?\s*$/i, "").trim();
      const sameListing =
        !item.label || (!!first.label && caption(first.label) === caption(item.label));
      // New titles, captions and intervening prose always end a listing. Geometry
      // supplies a continuation signal for older reports without model metadata.
      continues =
        sameListing &&
        ((!!item.continuation && item.page >= endPage && item.page <= endPage + 1) ||
          (item.page === endPage + 1 &&
            !!endBox &&
            !!startBox &&
            endBox.y1 >= 0.75 &&
            startBox.y0 <= 0.25 &&
            Math.abs(endBox.x0 - startBox.x0) < 0.03));
    }
    if (continues) run!.push(item);
    else runs.push([item]);
  }
  return runs;
}

export function listingLanguage(items: ReadingItem[]) {
  // A prompt may contain example Python; its enclosing listing remains prose.
  if (/\bprompt\b|提示词|提示模板/i.test(items[0]?.label || "")) return "plaintext";
  return (
    items.find((item) => item.code_language && item.code_language !== "plaintext")
      ?.code_language || "plaintext"
  );
}
