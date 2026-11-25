import type { PaperSummary } from "./api";
import { positionKey, readLocal, readingPosition } from "./readingExperience";
import type { DashboardReport, ReadingItem } from "./types";

export type LibraryView = "all" | "unread" | "reading" | "read" | "starred";
export type LibrarySort = "opened" | "added" | "year" | "title";
export const readingLabels = { unread: "未读", reading: "在读", read: "已读" };
export const paperTitle = (paper: PaperSummary) =>
  paper.title || paper.original_title || paper.source_name;

export function paperProgress(paper: PaperSummary): number | null {
  if (
    typeof paper.reading_progress === "number" &&
    Number.isFinite(paper.reading_progress)
  )
    return Math.min(1, Math.max(0, paper.reading_progress));
  const ids = paper.reading_item_ids || [];
  if (!paper.reading_source_version || !ids.length) return null;
  const position = readingPosition(
    readLocal(positionKey(paper.reading_source_version)),
    new Set(ids),
  );
  if (!position) return null;
  return (ids.indexOf(position.id) + position.fraction) / ids.length;
}

export function libraryPapers(
  papers: PaperSummary[],
  view: LibraryView,
  query: string,
  sort: LibrarySort,
): PaperSummary[] {
  const q = query.trim().toLocaleLowerCase();
  const shown = papers.filter((paper) => {
    const status = paper.reading_status || "unread";
    if (view === "starred" ? !paper.starred : view !== "all" && status !== view)
      return false;
    return (
      !q ||
      [
        paperTitle(paper),
        paper.source_name,
        ...(paper.authors || []),
        paper.year,
        paper.venue,
        paper.abstract,
        paper.doi,
      ]
        .join(" ")
        .toLocaleLowerCase()
        .includes(q)
    );
  });
  return shown.sort((a, b) => {
    if (sort === "title") return paperTitle(a).localeCompare(paperTitle(b), "zh-CN");
    if (sort === "year") return (b.year || "").localeCompare(a.year || "");
    const key = (paper: PaperSummary) =>
      sort === "added" ? paper.created_at : paper.last_opened || paper.created_at;
    return (key(b) || "").localeCompare(key(a) || "");
  });
}

/** Extract publication text only. The analysis title and summary are never metadata. */
export function readPaperSummary(report: DashboardReport, id = "local"): PaperSummary {
  const blocks = new Map(report.document.blocks.map((block) => [block.id, block]));
  const text = (item: ReadingItem) =>
    item.content ??
    item.parts
      .map(
        (part) =>
          part.join + (blocks.get(part.block_id)?.text.slice(part.start, part.end) || ""),
      )
      .join("");
  const info = (category: string) =>
    (report.reading?.information_items?.[category] || [])
      .map(text)
      .map((value) => value.trim())
      .filter(Boolean);
  const title =
    info("titles")[0] ||
    (/^[\da-f]{24,}$/i.test(report.document.title) ? "" : report.document.title) ||
    report.document.source_name.replace(/\.pdf$/i, "") ||
    "未命名论文";
  const authorRow = (report.reading?.information_items?.authors || []).find(
    (item) => item.page === 1,
  );
  const authors = authorRow
    ? text(authorRow)
        .split(/[（(\n]/)[0]
        .split(/[,，;；]/)
        .map((name) =>
          name
            .trim()
            .replace(/[\d¹²³⁴⁵*†‡]+$/, "")
            .trim(),
        )
        .filter(Boolean)
    : [];
  const publication = [...info("publication"), ...info("citation")].join("\n");
  const citation = info("citation").join("\n");
  const citationVenue = citation.match(
    /\[[A-Z/]+\]\s*[.。]?\s*([^,，\n]{2,120})[,，]\s*((?:19|20)\d{2})\b/i,
  );
  const doi =
    publication.match(/\b10\.\d{4,9}\/[^\s<>]+/i)?.[0].replace(/[.,;。]+$/, "") || "";
  let abstract = "";
  const abstractMarker = /^(?:摘\s*要\s*[:：]?\s*|abstract\s*[:：—–-]\s*|abstract\s*\n)/i;
  const items = report.reading?.items || [];
  for (let index = 0; index < items.length; index++) {
    const item = items[index];
    const content = text(item).trim();
    if (item.kind === "paragraph" && abstractMarker.test(content)) {
      abstract = content.replace(abstractMarker, "").trim();
      break;
    }
    if (
      item.kind === "heading" &&
      /^(?:摘\s*要|abstract)$/i.test(content) &&
      items[index + 1]?.kind === "paragraph"
    ) {
      abstract = text(items[index + 1]).trim();
      break;
    }
  }
  if (!abstract) {
    const source = report.document.blocks.find((block) =>
      abstractMarker.test(block.text.trim()),
    );
    if (source) abstract = source.text.trim().replace(abstractMarker, "").trim();
  }
  const itemIds = [
    "article-opening",
    ...items.map((item) => item.id),
    "article-references",
    ...(report.reading?.references || []).map((item) => item.id),
    "article-information",
  ];
  return {
    id,
    title,
    original_title: title,
    alternate_title: info("titles").find((value) => value !== title) || "",
    source_name: report.document.source_name,
    authors,
    year: citationVenue?.[2] || publication.match(/\b(?:19|20)\d{2}\b/)?.[0] || "",
    venue: citationVenue?.[1].trim() || "",
    abstract,
    doi,
    source_url: doi ? `https://doi.org/${doi}` : "",
    thumbnail_url: report.previews.find((preview) => preview.page === 1)?.data_url || "",
    pdf_url: report.source_pdf?.data_url || "",
    reading_status: "unread",
    starred: false,
    last_opened: "",
    reading_source_version: report.reading?.source_version || "",
    reading_item_ids: itemIds,
    pages: report.document.pages.length,
    paper_type: report.analysis.paper_type,
    node_count: report.analysis.nodes.length,
    evidence_count: report.evidence.length,
    created_at: report.run.created_at,
    status: report.run.status,
    model: report.run.model,
  };
}
