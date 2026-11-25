import { compileGraph } from "../graph";
import type { Block, DashboardReport, Evidence, PaperNode } from "../types";
import type { Answer } from "./provider";
import type { LocalPage, LocalPdf } from "./pdf";

export interface Batch {
  answer: Answer;
  pages: LocalPage[];
}
const compact = (s: string) => s.normalize("NFKC").replace(/[\s\u00ad]+/gu, "");

/** Only original PDF text can produce a verified reference. Matching pieces keep their own source boxes. */
export function locateQuote(
  quote: string,
  blocks: Block[],
): { block: Block; text: string; exact: boolean }[] {
  const needle = compact(quote);
  if (needle.length < 4) return [];
  const exact = blocks.find((b) => b.text.includes(quote));
  if (exact) return [{ block: exact, text: quote, exact: true }];
  const parts = blocks.map((b) => compact(b.text));
  const start = parts.join("").indexOf(needle);
  if (start < 0) return [];
  let cursor = 0;
  return blocks.flatMap((block, i) => {
    const from = cursor;
    cursor += parts[i].length;
    if (cursor <= start || from >= start + needle.length) return [];
    if (from <= start && cursor >= start + needle.length)
      return [{ block, text: quote, exact: false }];
    return [{ block, text: block.text, exact: false }];
  });
}

export function makeReport(
  doc: Pick<LocalPdf, "source" | "filename">,
  batches: Batch[],
  model: string,
  seconds: number,
  requests: number,
): DashboardReport {
  const first = batches[0].answer;
  const title = first.title || doc.filename.replace(/\.pdf$/i, "");
  const nodes: PaperNode[] = [];
  const evidence: Evidence[] = [];
  const blocks: Block[] = [],
    pages: DashboardReport["document"]["pages"] = [];
  const edges: DashboardReport["analysis"]["edges"] = [];
  const summaries = [...new Set(batches.map((b) => b.answer.summary).filter(Boolean))];
  let unmatched = 0,
    scanCount = 0;
  const root: PaperNode = {
    id: "N1",
    kind: "theme",
    title: title.slice(0, 160),
    summary: summaries[0] || title,
    explanation: "",
    evidence_ids: [],
    origin: "reader_guide",
    support: "unverified",
    parent_id: null,
  };
  nodes.push(root);
  for (const { answer, pages: local } of batches) {
    for (const [idx, entry] of answer.pages.entries()) {
      const page = local[idx];
      pages.push(page.meta);
      const source = page.blocks.map((b) => ({ ...b, section: entry.section }));
      blocks.push(...source);
      if (!source.length) {
        scanCount++;
        if (entry.transcription)
          blocks.push({
            id: "ocr" + entry.page,
            page: entry.page,
            text: entry.transcription,
            raw_text: entry.transcription,
            bbox: { x0: 0, y0: 0, x1: 1, y1: 1 },
            order: 0,
            section: entry.section,
            kind: "text",
            text_source: "ocr",
            text_warnings: ["模型转录，请对照原页。"],
          });
      }
      for (const finding of entry.findings) {
        const matches = locateQuote(finding.quote, source);
        const ids = matches.map(({ block, text, exact }) => {
          const id = "E" + (evidence.length + 1);
          evidence.push({
            id,
            block_id: block.id,
            page: entry.page,
            page_label: page.meta.label,
            section: entry.section,
            quote: text,
            bbox: block.bbox,
            match: exact ? "exact" : "normalized",
          });
          return id;
        });
        if (!ids.length) unmatched++;
        const id = "N" + (nodes.length + 1);
        const explanation = ids.length
          ? finding.detail
          : [
              finding.detail,
              "PDF 第 " + entry.page + " 页，须对照原页核实。",
              finding.quote,
            ]
              .filter(Boolean)
              .join("\n");
        nodes.push({
          id,
          parent_id: root.id,
          kind: finding.kind,
          title: finding.title,
          summary: finding.detail,
          explanation,
          support: ids.length ? "supported" : "unverified",
          origin: "author",
          evidence_ids: ids,
        });
        edges.push({
          id: "R" + edges.length,
          source: root.id,
          target: id,
          label: "",
          relation: "contains",
        });
      }
    }
  }
  const warnings = [
    ...(unmatched
      ? [unmatched + " 条论点的引句未能与原 PDF 文字层逐字匹配，请对照原页。"]
      : []),
    ...(scanCount
      ? [scanCount + " 页缺少可读文字层，模型转录和这些页的论点需要人工核对。"]
      : []),
  ];
  const grounded = nodes.filter((n) => n.evidence_ids.length).length;
  const parser = "PDF.js · 模型页图直读";
  const report: DashboardReport = {
    schema_version: "1.0",
    source_pdf: { data_url: doc.source.data_url, sha256: doc.source.sha256 },
    document: {
      title,
      source_name: doc.filename,
      sha256: doc.source.sha256,
      parser,
      pages,
      blocks,
      warnings: [],
    },
    analysis: {
      title,
      summary: summaries.join("\n\n"),
      paper_type: "other",
      research_question:
        first.question || batches.find((b) => b.answer.question)?.answer.question || "",
      reading_guide: [],
      nodes,
      edges,
      glossary: [],
      warnings,
    },
    evidence,
    previews: [],
    graphs: [],
    quality: {
      total_pages: pages.length,
      parsed_pages: pages.length,
      text_chars: blocks.reduce((n, b) => n + b.text.length, 0),
      grounded_nodes: grounded,
      total_nodes: nodes.length,
      checks: ["已检查每批页码覆盖，并将模型引句与原 PDF 文字层比对。"],
      warnings,
    },
    run: {
      id: crypto.randomUUID().replaceAll("-", ""),
      created_at: new Date().toISOString(),
      model,
      parser,
      status: "complete",
      timings: { total: seconds },
      usage: {
        requests,
        prompt_tokens: batches.reduce((n, b) => n + b.answer.input, 0),
        completion_tokens: batches.reduce((n, b) => n + b.answer.output, 0),
      },
      prompt_version: "browser-vision-1",
    },
  };
  report.graphs = [
    {
      id: "overview",
      title: "研究导图",
      description: "按论文页序组织的论点与证据。",
      node_ids: nodes.map((n) => n.id),
      edge_ids: edges.map((e) => e.id),
      mermaid: "",
      validated: false,
    },
  ];
  report.graphs[0].mermaid = compileGraph(report, report.graphs[0]);
  return report;
}
