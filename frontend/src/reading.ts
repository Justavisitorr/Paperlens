import type {
  Block,
  DashboardReport,
  ReadingDocument,
  ReadingItem,
  ReadingPart,
  ReadingTranscription,
} from "./types";
import { normalizeCaptionHeading } from "./readingStructure";

export const partText = (item: ReadingItem, blocks: Map<string, Block>) =>
  item.parts
    .map((p) => p.join + (blocks.get(p.block_id)?.text.slice(p.start, p.end) || ""))
    .join("");
/** Corrected text has no implicit mapping to the original block's offsets. */
export const transcriptionText = (transcription: ReadingTranscription) =>
  transcription.segments
    .map((segment) => segment.formula?.latex || segment.text)
    .join("");
export const readingText = (item: ReadingItem, blocks: Map<string, Block>) =>
  item.content ??
  (item.transcription ? transcriptionText(item.transcription) : partText(item, blocks));
/** Only an exact original projection can keep UTF-16 selection coordinates. */
export const usesSourceOffsets = (item: ReadingItem, blocks: Map<string, Block>) =>
  item.content != null ? item.content === partText(item, blocks) : !item.transcription;
export const referenceNumber = (item: ReadingItem) =>
  (item.reference_number || item.label).replace(/[\[\]]/g, "").trim();

const referenceTextKey = (text: string) =>
  text
    .normalize("NFKD")
    .replace(/[\s\p{M}]/gu, "")
    .replace(/[–—−‐‑]/g, "-")
    .replace(/[‘’]/g, "'")
    .replace(/[“”]/g, '"');

/** Old reports can contain punctuation/diacritic leftovers from source coverage. */
export function cleanReferences(
  items: ReadingItem[],
  blocks: Map<string, Block>,
): ReadingItem[] {
  const corrected = items.flatMap((item) => {
    if (item.content == null || !item.parts.length) return [];
    const bounds = new Map<string, ReadingPart>();
    for (const part of item.parts) {
      const old = bounds.get(part.block_id);
      bounds.set(
        part.block_id,
        old
          ? {
              ...old,
              start: Math.min(old.start, part.start),
              end: Math.max(old.end, part.end),
            }
          : { ...part },
      );
    }
    const source = [...bounds.values()]
      .map((p) => blocks.get(p.block_id)?.text.slice(p.start, p.end) || "")
      .join("");
    return referenceTextKey(source) === referenceTextKey(item.content)
      ? [{ item, bounds }]
      : [];
  });
  return items.filter((item) => {
    const text = readingText(item, blocks).replace(
      /^\s*\[(?:\d{1,5}|[A-Z][A-Za-z0-9]{0,23}\+?)\]\s*/,
      "",
    );
    if (!/[\p{L}\p{N}]/u.test(referenceTextKey(text))) return false;
    if (
      !item.id.startsWith("source-") ||
      item.content != null ||
      item.transcription ||
      !item.parts.length
    )
      return true;
    return !corrected.some(
      ({ item: other, bounds }) =>
        (!referenceNumber(item) || referenceNumber(item) === referenceNumber(other)) &&
        item.parts.every((p) => {
          const range = bounds.get(p.block_id);
          return range && range.start <= p.start && p.start < p.end && p.end <= range.end;
        }),
    );
  });
}

/** New slice records are authoritative, including an intentionally empty list. */
export function informationParts(
  reading: ReadingDocument,
  blocks: Map<string, Block>,
): Record<string, ReadingPart[]> {
  const keys = new Set([
    ...Object.keys(reading.information),
    ...Object.keys(reading.information_parts || {}),
  ]);
  return Object.fromEntries(
    [...keys].map((key) => [
      key,
      reading.information_parts?.[key] ??
        (reading.information[key] || []).flatMap((id) => {
          const b = blocks.get(id);
          return b
            ? [{ block_id: id, start: 0, end: b.text.length, join: "", bbox: b.bbox }]
            : [];
        }),
    ]),
  );
}
export const sourceLabel = (report: DashboardReport, pages: number[]) => {
  const unique = [...new Set(pages)];
  const labels = unique.map(
    (n) => report.document.pages.find((p) => p.number === n)?.label || "",
  );
  return `${labels.every(Boolean) ? `原文 ${labels.join("、")} · ` : ""}PDF ${unique.join("、")}`;
};
export const safeLink = (url: string) => (/^https?:\/\//i.test(url) ? url : "");

/** Older portable reports remain readable; new runs carry the full server projection. */
export function readingDocument(report: DashboardReport): ReadingDocument {
  const sourceBlocks = new Map(report.document.blocks.map((b) => [b.id, b]));
  const normalize = (item: ReadingItem) =>
    normalizeCaptionHeading(item, readingText(item, sourceBlocks).trim(), sourceBlocks);
  if (report.reading)
    return {
      ...report.reading,
      items: report.reading.items.map(normalize),
      references: cleanReferences(report.reading.references || [], sourceBlocks),
      information: report.reading.information || {},
      hidden_ids: report.reading.hidden_ids || [],
      warnings: report.reading.warnings || [],
    };
  const out: ReadingDocument = {
    version: "legacy",
    source_version: report.run.id,
    items: [],
    references: [],
    information: {},
    hidden_ids: [],
    warnings: report.source_pdf?.data_url
      ? []
      : ["此旧报告未内置原版 PDF；文字来源保留原块位置，需提供原文件核对。"],
  };
  let bibliography = false;
  let previousReference: Block | undefined;
  let referenceClosed = false;
  const blocks = new Map(report.document.blocks.map((b) => [b.id, b]));
  for (const b of report.document.blocks) {
    if (["header", "footer"].includes(b.kind)) {
      out.hidden_ids.push(b.id);
      continue;
    }
    if (b.kind === "heading")
      bibliography = /^(参考文献|references|bibliography)[:：]?$/i.test(b.text.trim());
    if (bibliography && b.kind === "heading") {
      out.hidden_ids.push(b.id);
      continue;
    }
    const reference = bibliography || b.kind === "reference";
    const item: ReadingItem = {
      id: `reading-${b.id}`,
      kind: reference
        ? "reference"
        : b.kind === "heading"
          ? "heading"
          : b.kind === "formula"
            ? "formula"
            : b.kind === "table"
              ? "table"
              : "paragraph",
      parts: [{ block_id: b.id, start: 0, end: b.text.length, join: "" }],
      level: b.heading_level || 1,
      label: b.kind === "heading" ? b.text : "",
      asset: "",
      page: b.page,
      bbox: b.bbox,
      result_ids: [],
      visual_ids: [],
    };
    if (reference) {
      const leading = b.text.match(/^\s*\[(\d{1,5}|[A-Z][A-Za-z0-9]{0,23}\+?)\]\s*/);
      const trailing = leading ? null : b.text.match(/\s*\[(\d+)\]\s*$/);
      if (leading || trailing) {
        const match = leading || trailing!;
        item.label = `[${match[1]}]`;
        if (leading) item.parts[0].start = match[0].length;
        else item.parts[0].end = match.index!;
      }
      const last = out.references.at(-1);
      const a = previousReference;
      // Geometry supplies the continuation boundary; no word repair is inferred.
      const adjacent = a && b.page - a.page >= 0 && b.page - a.page <= 1;
      const cross =
        a &&
        (b.page > a.page || (a.bbox.x1 <= b.bbox.x0 + 0.05 && b.bbox.y0 < a.bbox.y0));
      const shortGap =
        a &&
        b.page === a.page &&
        Math.abs(a.bbox.x0 - b.bbox.x0) < 0.08 &&
        b.bbox.y0 - a.bbox.y1 >= 0 &&
        b.bbox.y0 - a.bbox.y1 < 0.025;
      if (
        last &&
        adjacent &&
        (cross || shortGap) &&
        !referenceClosed &&
        !leading &&
        (!trailing || !last.label)
      ) {
        const tail = partText(last, blocks);
        item.parts[0].join = /-\s*$/.test(tail) ? "" : " ";
        last.parts.push(...item.parts);
        if (trailing) last.label = item.label;
      } else {
        out.references.push(item);
      }
      previousReference = b;
      referenceClosed = Boolean(trailing);
    } else {
      previousReference = undefined;
      out.items.push(normalize(item));
    }
  }
  for (const item of out.references) {
    const text = partText(item, blocks);
    const doi = text.match(/10\.\d{4,9}\/[^\s<>]+/i)?.[0]?.replace(/[.,;。]+$/, "");
    const url = text.match(/https?:\/\/[^\s<>]+/i)?.[0]?.replace(/[.,;。]+$/, "");
    item.href = doi
      ? `https://doi.org/${encodeURIComponent(doi).replace(/%2F/gi, "/")}`
      : url || "";
  }
  const byBlock = new Map(
    out.items.flatMap((item) => item.parts.map((part) => [part.block_id, item] as const)),
  );
  const byEvidence = new Map(report.evidence.map((e) => [e.id, e.block_id]));
  const visuals = new Map((report.visuals || []).map((v) => [v.id, v]));
  for (const visual of visuals.values()) {
    const target = visual.source_block_ids.map((id) => byBlock.get(id)).find(Boolean);
    if (target) target.visual_ids.push(visual.id);
  }
  const attached = new Set<string>();
  for (const result of report.quantitative_results || []) {
    if (attached.has(result.id)) continue;
    const evidenceTarget = result.evidence_ids
      .map((id) => byBlock.get(byEvidence.get(id) || ""))
      .find(Boolean);
    const visual = visuals.get(result.visual_id || "");
    const visualTarget = visual?.source_block_ids
      .map((id) => byBlock.get(id))
      .find(Boolean);
    const pageTarget = out.items.find(
      (item) => item.page === result.page && item.kind !== "heading",
    );
    let target = evidenceTarget || visualTarget || pageTarget;
    if (!target) {
      target = {
        id: `results-${result.id}`,
        kind: "paragraph",
        parts: [],
        level: 1,
        label: "本页定量记录（未定位到具体图表）",
        asset: "",
        page: result.page,
        result_ids: [],
        visual_ids: [],
      };
      const slot = out.items.findIndex((item) => item.page > result.page);
      out.items.splice(slot < 0 ? out.items.length : slot, 0, target);
    }
    target.result_ids.push(result.id);
    attached.add(result.id);
  }
  out.references = cleanReferences(out.references, sourceBlocks);
  return out;
}
