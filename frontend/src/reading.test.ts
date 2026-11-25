import { describe, expect, it } from "vitest";
import {
  informationParts,
  partText,
  readingDocument,
  readingText,
  safeLink,
  sourceLabel,
  transcriptionText,
  usesSourceOffsets,
  referenceNumber,
  cleanReferences,
} from "./reading";
import type {
  Block,
  DashboardReport,
  Evidence,
  QuantitativeResult,
  ReadingDocument,
  ReadingItem,
  VisualEvidence,
} from "./types";

const block = (id: string, text: string, page = 1, kind = "paragraph"): Block => ({
  id,
  text,
  raw_text: text,
  page,
  kind,
  order: 0,
  section: "",
  bbox: { x0: 0.1, y0: 0.1, x1: 0.4, y1: 0.2 },
});
const fixture = (blocks: Block[] = []): DashboardReport => ({
  schema_version: "legacy",
  source_pdf: null,
  document: {
    title: "Legacy paper",
    source_name: "paper.pdf",
    sha256: "",
    parser: "legacy",
    blocks,
    warnings: [],
    pages: [1, 2, 3, 4, 5].map((number) => ({
      number,
      width: 600,
      height: 800,
      label: number === 1 ? "i" : "",
      status: "parsed",
      text_chars: 0,
      warnings: [],
    })),
  },
  analysis: {
    title: "",
    summary: "",
    paper_type: "other",
    research_question: "",
    reading_guide: [],
    nodes: [],
    edges: [],
    glossary: [],
    warnings: [],
  },
  evidence: [],
  graphs: [],
  previews: [],
  quality: {
    total_pages: 5,
    parsed_pages: 5,
    text_chars: 0,
    grounded_nodes: 0,
    total_nodes: 0,
    checks: [],
    warnings: [],
  },
  run: {
    id: "legacy-run",
    created_at: "",
    model: "",
    parser: "legacy",
    status: "complete",
    timings: {},
    usage: { requests: 0, prompt_tokens: 0, completion_tokens: 0 },
    prompt_version: "",
  },
});
const result = (id: string, page: number): QuantitativeResult => ({
  id,
  page,
  metric: "accuracy",
  method: "method",
  dataset: "benchmark",
  value: 0.9,
  value_text: "0.9",
  unit: "",
  condition: "",
  figure_label: "",
  source: "text",
  precision: "reported",
  evidence_ids: [],
  notes: "",
});

describe("reading source helpers", () => {
  it("hides empty references and only removes source residues represented by the same corrected entry", () => {
    const raw = block("ref", "A. Novikov, N. V˜ u. Alphaevolve. 2025.");
    const blocks = new Map([[raw.id, raw]]);
    const start = raw.text.indexOf("˜"),
      end = start + 3;
    const entry = {
      id: "edition-ref",
      kind: "reference",
      label: "",
      content: "A. Novikov, N. Vũ. Alphaevolve. 2025.",
      parts: [
        { block_id: raw.id, start: 0, end: start, join: "" },
        { block_id: raw.id, start: end, end: raw.text.length, join: "" },
      ],
    } as ReadingItem;
    const residue = {
      ...entry,
      id: "source-accent",
      content: null,
      parts: [{ block_id: raw.id, start, end, join: "" }],
    };
    const empty = { ...entry, id: "empty", content: " \n - — ", parts: [] };
    const labelOnly = { ...empty, id: "label", content: "[17]" };
    const short = { ...empty, id: "short", content: "WHO." };
    const input = [entry, residue, empty, labelOnly, short];
    const saved = JSON.stringify(input);
    expect(cleanReferences(input, blocks).map((r) => r.id)).toEqual([entry.id, short.id]);
    expect(JSON.stringify(input)).toBe(saved);
    expect(cleanReferences([residue], blocks)).toEqual([residue]);
    expect(
      cleanReferences([{ ...entry, content: "A different paper." }, residue], blocks),
    ).toHaveLength(2);
  });
  it("prefers semantic content and only retains offsets for exact source text", () => {
    const b = block("raw", "A😀BC");
    const source = new Map([[b.id, b]]);
    const item = {
      parts: [{ block_id: b.id, start: 0, end: b.text.length, join: "" }],
    } as ReadingItem;
    expect(readingText(item, source)).toBe(b.text);
    expect(usesSourceOffsets(item, source)).toBe(true);
    expect(usesSourceOffsets({ ...item, content: b.text }, source)).toBe(true);
    expect(usesSourceOffsets({ ...item, content: "A $x$" }, source)).toBe(false);
    expect(readingText({ ...item, content: "" }, source)).toBe("");
    expect(referenceNumber({ ...item, label: "[1]", reference_number: "2" })).toBe("2");
    expect(referenceNumber({ ...item, label: "[7]" })).toBe("7");
  });
  it("reads corrected transcription while retaining untouched UTF-16 source slices", () => {
    const b = block("raw", "broken ∑p");
    const item = {
      id: "transcribed",
      kind: "algorithm",
      page: 1,
      level: 1,
      label: "",
      asset: "",
      result_ids: [],
      visual_ids: [],
      parts: [{ block_id: b.id, start: 0, end: b.text.length, join: "" }],
      transcription: {
        source: "docling",
        verified: false,
        warnings: [],
        segments: [
          { text: "输入：x\n1. 更新 " },
          {
            text: "damaged fallback",
            formula: {
              latex: "x_i^2",
              mathml: "",
              source: "docling",
              status: "renderable",
              verified: false,
              warnings: [],
            },
          },
          { text: "\n2. 返回" },
        ],
      },
    } as ReadingItem;
    const blocks = new Map([[b.id, b]]);
    expect(readingText(item, blocks)).toBe("输入：x\n1. 更新 x_i^2\n2. 返回");
    expect(transcriptionText(item.transcription!)).toBe(readingText(item, blocks));
    expect(partText(item, blocks)).toBe(b.text);
    expect(b.text).toBe("broken ∑p");
  });

  it("prefers exact metadata slices, supports new categories and falls back for older categories", () => {
    const mixed = block("mixed", "基金：123。正文继续。");
    const authors = block("authors", "作者 A");
    const bbox = { x0: 0.1, y0: 0.7, x1: 0.4, y1: 0.72 };
    const report = fixture([mixed, authors]);
    report.reading = {
      version: "2",
      source_version: "source",
      items: [],
      references: [],
      information: {
        declarations: [mixed.id],
        authors: [authors.id],
        titles: [mixed.id],
      },
      information_parts: {
        declarations: [{ block_id: mixed.id, start: 0, end: 7, join: "", bbox }],
        titles: [],
        footnotes: [{ block_id: mixed.id, start: 7, end: 9, join: "" }],
      },
      hidden_ids: [],
      warnings: [],
    };
    const parts = informationParts(
      report.reading,
      new Map(report.document.blocks.map((b) => [b.id, b])),
    );
    expect(parts.declarations[0].bbox).toEqual(bbox);
    expect(
      partText(
        { parts: parts.declarations } as ReadingItem,
        new Map([[mixed.id, mixed]]),
      ),
    ).toBe("基金：123。");
    expect(parts.authors[0]).toMatchObject({
      block_id: authors.id,
      start: 0,
      end: authors.text.length,
    });
    expect(parts.titles).toEqual([]);
    expect(parts.footnotes).toHaveLength(1);
  });

  it("uses UTF-16 source offsets around emoji and preserves joins", () => {
    const b = block("raw", "A😀BC");
    const item = {
      parts: [
        { block_id: b.id, start: 1, end: 3, join: "" },
        { block_id: b.id, start: 3, end: 5, join: " · " },
      ],
    } as ReadingItem;
    expect(partText(item, new Map([[b.id, b]]))).toBe("😀 · BC");
  });

  it("does not invent original-page labels when any requested label is unknown", () => {
    const report = fixture();
    expect(sourceLabel(report, [1, 1])).toBe("原文 i · PDF 1");
    expect(sourceLabel(report, [1, 2, 2])).toBe("PDF 1、2");
    expect(sourceLabel(report, [99])).toBe("PDF 99");
  });

  it("only exposes explicit HTTP or HTTPS links", () => {
    expect(safeLink("https://doi.org/10.1000/test")).toBe("https://doi.org/10.1000/test");
    expect(safeLink("HTTP://example.org/paper")).toBe("HTTP://example.org/paper");
    for (const url of [
      "javascript:alert(1)",
      "data:text/html,test",
      "//example.org",
      "file:///paper.pdf",
    ])
      expect(safeLink(url)).toBe("");
  });
});

describe("legacy reading projection", () => {
  it("retains body spans, identifies reference blocks without a heading, and warns about absent PDF", () => {
    const report = fixture([
      block("body", "正文😀完整"),
      block("table", "raw table", 1, "table"),
      block("reference", "[1] Source paper", 2, "reference"),
    ]);
    const projection = readingDocument(report);
    expect(
      projection.items.flatMap((item) => item.parts.map((part) => part.block_id)),
    ).toEqual(["body", "table"]);
    expect(projection.references[0].label).toBe("[1]");
    expect(projection.items[0].parts[0]).toEqual({
      block_id: "body",
      start: 0,
      end: 6,
      join: "",
    });
    expect(projection.warnings.join()).toContain("未内置原版 PDF");
    report.source_pdf = { data_url: "data:application/pdf;base64,eA==", sha256: "" };
    expect(readingDocument(report).warnings).toEqual([]);
  });

  it("joins numbered references across pages and columns without deleting raw hyphens", () => {
    const first = block("first", "[1] 😀 Context-", 1);
    const next = block("next", "aware systems", 2);
    next.bbox = { x0: 0.1, y0: 0.6, x1: 0.4, y1: 0.7 };
    const column = block("column", "doi: 10.1000/xyz.", 2);
    column.bbox = { x0: 0.6, y0: 0.1, x1: 0.9, y1: 0.2 };
    const report = fixture([
      block("refs", "References", 1, "heading"),
      first,
      next,
      column,
      block("second", "[2] Another paper", 2),
    ]);
    const projection = readingDocument(report);
    expect(projection.references).toHaveLength(2);
    const citation = projection.references[0];
    expect(citation.parts.map((part) => part.block_id)).toEqual([
      "first",
      "next",
      "column",
    ]);
    expect(citation.parts[0]).toEqual({
      block_id: "first",
      start: 4,
      end: first.text.length,
      join: "",
    });
    expect(
      partText(citation, new Map(report.document.blocks.map((b) => [b.id, b]))),
    ).toBe("😀 Context-aware systems doi: 10.1000/xyz.");
    expect(citation.href).toBe("https://doi.org/10.1000/xyz");
  });

  it("joins a trailing-number continuation and starts a new record after its closing number", () => {
    const first = block("first", "RAO C R. Linear models", 1, "reference");
    const ending = block("ending", "Springer, 1995: 3–18. [1]", 1, "reference");
    ending.bbox = { x0: 0.1, y0: 0.21, x1: 0.4, y1: 0.3 };
    const next = block(
      "next",
      "Another paper https://example.org/paper. [2]",
      2,
      "reference",
    );
    const report = fixture([first, ending, next]);
    const projection = readingDocument(report);
    expect(projection.references).toHaveLength(2);
    expect(projection.references[0].label).toBe("[1]");
    expect(projection.references[0].parts.map((part) => part.block_id)).toEqual([
      "first",
      "ending",
    ]);
    expect(projection.references[1].href).toBe("https://example.org/paper");
  });

  it("encodes DOI path characters rather than interpreting them as URL fragments", () => {
    const projection = readingDocument(
      fixture([block("doi", "[1] DOI: 10.1000/a#b.", 1, "reference")]),
    );
    expect(projection.references[0].href).toBe("https://doi.org/10.1000/a%23b");
  });

  it("attaches every quantitative record once, preferring explicit sources over page fallback", () => {
    const report = fixture([
      block("heading", "Results", 1, "heading"),
      block("evidence-block", "text results", 2),
      block("visual-block", "table results", 3, "table"),
      block("page-block", "fallback text", 4),
      block("ref", "[1] Paper", 5, "reference"),
    ]);
    report.evidence = [{ id: "e1", block_id: "evidence-block" } as Evidence];
    report.visuals = [
      { id: "v1", page: 3, source_block_ids: ["visual-block"] } as VisualEvidence,
    ];
    report.quantitative_results = [
      { ...result("q1", 3), evidence_ids: ["e1"], visual_id: "v1" },
      { ...result("q2", 4), visual_id: "v1" },
      result("q3", 4),
      result("q4", 5),
      result("q3", 4),
    ];
    const projection = readingDocument(report);
    const assigned = new Map(
      projection.items.flatMap((item) =>
        item.result_ids.map((id) => [id, item] as const),
      ),
    );
    expect(projection.items.flatMap((item) => item.result_ids)).toEqual([
      "q1",
      "q2",
      "q3",
      "q4",
    ]);
    expect(assigned.get("q1")?.parts[0].block_id).toBe("evidence-block");
    expect(assigned.get("q2")?.parts[0].block_id).toBe("visual-block");
    expect(assigned.get("q3")?.parts[0].block_id).toBe("page-block");
    expect(assigned.get("q4")?.parts).toEqual([]);
    expect(assigned.get("q4")?.label).toContain("未定位到具体图表");
    expect(assigned.get("q4")?.page).toBe(5);
    expect(projection.references.every((item) => !item.result_ids.length)).toBe(true);
  });

  it("keeps existing reading projections authoritative and defaults missing optional collections", () => {
    const report = fixture([block("must-not-reproject", "raw text")]);
    report.reading = {
      version: "existing",
      source_version: "server-source",
      items: [],
    } as unknown as ReadingDocument;
    expect(readingDocument(report)).toEqual({
      version: "existing",
      source_version: "server-source",
      items: [],
      references: [],
      information: {},
      hidden_ids: [],
      warnings: [],
    });
  });
});
