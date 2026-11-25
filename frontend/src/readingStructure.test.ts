import { describe, expect, it } from "vitest";
import { captionKind, sectionHeadings } from "./readingStructure";
import { readingDocument } from "./reading";
import type { Block, DashboardReport, ReadingItem } from "./types";

describe("reading section boundaries", () => {
  it.each([
    ["Figure 2: The two bounding nodes of a light-supported bridge.", "figure"],
    ["Fig. S1. Supplementary result", "figure"],
    ["Table 1: Notation used across the proof.", "table"],
    ["图 2：实验结果", "figure"],
    ["图2实验结果", "figure"],
    ["表 1 实验数据", "table"],
    ["Tab. 1: Results", "table"],
    ["Figure 1a: Subplot caption", "figure"],
    ["7.2 The corrected cubic", null],
    ["Figures and tables", null],
    ["Table-driven methods", null],
    ["Figure preparation", null],
  ])("recognizes numbered captions without using length: %s", (text, kind) => {
    expect(captionKind(text!)).toBe(kind);
  });

  it("repairs old caption headings without modifying source data or IDs", () => {
    const text =
      "Figure 2: The two bounding nodes. " + "Original caption sentence. ".repeat(20);
    const block = { id: "caption", text, kind: "caption" } as Block;
    const old = {
      id: "saved-caption",
      kind: "heading",
      label: text,
      content: text,
      parts: [{ block_id: block.id, start: 0, end: text.length, join: "" }],
    } as ReadingItem;
    const heading = {
      ...old,
      id: "real-section",
      label: "7.2 The corrected cubic",
      content: "7.2 The corrected cubic",
      parts: [],
    };
    const r = {
      document: { blocks: [block] },
      reading: { items: [heading, old] },
    } as DashboardReport;
    const fixed = readingDocument(r);
    expect(sectionHeadings(fixed.items).map((item) => item.id)).toEqual(["real-section"]);
    expect(fixed.items[1]).toMatchObject({
      id: old.id,
      kind: "figure",
      content: text,
      parts: old.parts,
    });
    expect(old.kind).toBe("heading");
    expect(block.text).toBe(text);
    expect(heading).toBe(fixed.items[0]);
  });

  it("keeps objects out of chapter navigation and retains long genuine headings", () => {
    const heading = {
      id: "heading",
      kind: "heading",
      label: "A long genuine section title ".repeat(15),
    } as ReadingItem;
    const objects = ["figure", "table", "algorithm", "code"].map(
      (kind) => ({ id: kind, kind }) as ReadingItem,
    );
    expect(sectionHeadings([heading, ...objects])).toEqual([heading]);
  });
});
