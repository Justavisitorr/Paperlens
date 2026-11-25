import { describe, expect, it } from "vitest";
import { toMarkdown, toPlainText } from "./export";
import type { Block, DashboardReport, ReadingItem } from "./types";

const block = (id: string, text: string, kind = "text"): Block => ({
  id,
  text,
  raw_text: `uncorrected ${id}`,
  kind,
  page: 1,
  order: 0,
  section: "",
  bbox: { x0: 0, y0: 0, x1: 1, y1: 1 },
});
const fixture = (blocks: Block[] = []) =>
  ({
    analysis: {
      title: "Paper",
      summary: "",
      research_question: "",
      nodes: [],
      glossary: [],
      warnings: [],
    },
    document: { title: "Original paper", blocks, pages: [], warnings: [] },
    evidence: [],
    run: { id: "export-test" },
    quality: { warnings: [] },
  }) as unknown as DashboardReport;
const item = (id: string, patch: Partial<ReadingItem> = {}): ReadingItem => ({
  id,
  kind: "paragraph",
  parts: [],
  page: 1,
  label: "",
  level: 1,
  asset: "",
  result_ids: [],
  visual_ids: [],
  ...patch,
});

describe("plain-text report export", () => {
  it("uses normalized legacy reading text without changing the raw evidence", () => {
    const report = fixture([
      block("head", "Running header", "header"),
      block("body", "Corrected text"),
    ]);
    const before = JSON.stringify(report);
    expect(toPlainText(report)).toContain("[PDF 第 1 页]\nCorrected text");
    expect(toPlainText(report)).not.toContain("uncorrected");
    expect(toPlainText(report)).not.toContain("Running header");
    expect(JSON.stringify(report)).toBe(before);
  });

  it("keeps corrected content, code indentation, reference numbers and publication text", () => {
    const report = fixture([
      block("old", "old projection"),
      block("authors", "Author Alice"),
    ]);
    report.reading = {
      version: "1",
      source_version: "test",
      hidden_ids: [],
      warnings: [],
      items: [
        item("body", {
          content: "Faithful merged text",
          source_pages: [1, 2],
          parts: [{ block_id: "old", start: 0, end: 14, join: "" }],
        }),
        item("code", {
          kind: "code",
          content: "for x in values:\n    print(x)\n    print('done')",
        }),
        item("transcribed", {
          transcription: {
            source: "vision",
            verified: true,
            warnings: [],
            segments: [{ text: "Corrected transcription\nsecond line" }],
          },
        }),
      ],
      references: [
        item("ref", { kind: "reference", label: "[7]", content: "A cited work" }),
      ],
      information: { authors: ["authors"], publication: ["old"] },
      information_parts: {
        authors: [{ block_id: "authors", start: 7, end: 12, join: "" }],
      },
      information_items: {
        publication: [item("publication", { content: "Published in 2026" })],
      },
    };
    const before = JSON.stringify(report);
    const text = toPlainText(report);
    expect(text).toContain("[PDF 第 1、2 页]\nFaithful merged text");
    expect(text).toContain("for x in values:\n    print(x)\n    print('done')");
    expect(text).toContain("Corrected transcription\nsecond line");
    expect(text).toContain("[7] A cited work");
    expect(text).toContain("[PDF 第 1 页] 作者信息\nAlice");
    expect(text).toContain("Published in 2026");
    expect(text).not.toMatch(/uncorrected|old projection/);
    expect(JSON.stringify(report)).toBe(before);
  });

  it("respects deliberately empty projections and never duplicates reference labels", () => {
    const report = fixture([block("old", "Hidden stale text")]);
    report.reading = {
      version: "1",
      source_version: "test",
      hidden_ids: [],
      warnings: [],
      items: [
        item("blank", {
          content: "",
          parts: [{ block_id: "old", start: 0, end: 17, join: "" }],
        }),
      ],
      references: [
        item("ref", { kind: "reference", label: "[2]", content: "[2] Cited text" }),
      ],
      information: { authors: ["old"], titles: ["old"] },
      information_parts: { authors: [] },
      information_items: { titles: [] },
    };
    const text = toPlainText(report);
    expect(text).not.toMatch(/Hidden stale text|作者信息|其他标题/);
    expect(text.match(/\[2\]/g)).toHaveLength(1);
  });
});

it("keeps report evidence and multi-line glossary/warnings inside Markdown containers", () => {
  const report = fixture();
  report.analysis.nodes = [
    {
      id: "n",
      title: "Claim",
      summary: "",
      explanation: "",
      kind: "theme",
      parent_id: null,
      origin: "author",
      support: "supported",
      evidence_ids: ["e"],
    },
  ];
  report.evidence = [
    {
      id: "e",
      block_id: "b",
      page: 1,
      source_pages: [1, 2],
      section: "",
      quote: "first\r\n\r\nsecond\rthird",
    } as DashboardReport["evidence"][number],
  ];
  report.analysis.glossary = [
    { term: "Term", explanation: "one\n\ntwo", evidence_ids: [], support: "supported" },
  ];
  report.quality.warnings = ["first\r\nsecond"];
  const text = toMarkdown(report);
  expect(text).toContain("> first\n> \n> second\n> third");
  expect(text).toContain("— PDF 第 1–2 页");
  expect(text).toContain("- **Term**：one\n  \n  two");
  expect(text).toContain("- first\n  second");
});
