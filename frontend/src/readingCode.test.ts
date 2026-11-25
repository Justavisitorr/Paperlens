import { describe, expect, it } from "vitest";
import { listingLanguage, readingRuns } from "./readingCode";
import { highlightCodeParts } from "./codeHighlight";
import type { ReadingItem } from "./types";

const item = (
  id: string,
  page: number,
  extra: Partial<ReadingItem> = {},
): ReadingItem => ({
  id,
  page,
  kind: "code",
  content: id,
  code_language: "plaintext",
  parts: [],
  level: 1,
  label: "",
  asset: "",
  result_ids: [],
  visual_ids: [],
  bbox: { x0: 0.1, y0: 0.1, x1: 0.9, y1: 0.9 },
  ...extra,
});
const group = (items: ReadingItem[]) =>
  readingRuns(items, new Map()).map((run) => run.map((i) => i.id));

describe("continuous code listings", () => {
  it("joins multiple page tails without changing stored text, IDs or note anchors", () => {
    const items = [
      item("first", 1, { label: "Listing 3", code_language: "cpp" }),
      item("next", 2),
      item("last", 3),
    ];
    const before = JSON.stringify(items);
    expect(group(items)).toEqual([["first", "next", "last"]]);
    expect(listingLanguage(items)).toBe("cpp");
    expect(JSON.stringify(items)).toBe(before);
  });
  it("keeps new headings, prose, listing captions and page gaps separate", () => {
    expect(
      group([item("a", 1), item("head", 2, { kind: "heading" }), item("b", 2)]),
    ).toEqual([["a"], ["head"], ["b"]]);
    expect(
      group([item("a", 1), item("body", 2, { kind: "paragraph" }), item("b", 2)]),
    ).toEqual([["a"], ["body"], ["b"]]);
    expect(
      group([
        item("a", 1, { label: "Listing 1" }),
        item("b", 2, { label: "Listing 2", continuation: true }),
      ]),
    ).toEqual([["a"], ["b"]]);
    expect(group([item("a", 1), item("b", 3)])).toEqual([["a"], ["b"]]);
    expect(
      group([
        item("a", 1),
        item("b", 2, { bbox: { x0: 0.1, y0: 0.5, x1: 0.9, y1: 0.9 } }),
      ]),
    ).toEqual([["a"], ["b"]]);
    expect(group([item("a", 1), item("b", 1)])).toEqual([["a"], ["b"]]);
  });
  it("allows explicit continuations and repeated continuation captions", () => {
    expect(group([item("a", 1), item("b", 1, { continuation: true })])).toEqual([
      ["a", "b"],
    ]);
    expect(
      group([
        item("a", 1, { label: "Listing 1" }),
        item("b", 2, { label: "Listing 1 (continued)" }),
      ]),
    ).toEqual([["a", "b"]]);
  });
  it("keeps prompt listings plain even if a later page includes Python examples", () => {
    expect(
      listingLanguage([
        item("a", 1, { label: "Listing 2 | Policy prompt" }),
        item("b", 2, { code_language: "python" }),
      ]),
    ).toBe("plaintext");
  });
});

describe("cross-page syntax color", () => {
  it("preserves multiline strings and comments across original item boundaries", () => {
    const parts = ['value = """first', "second <tag> & 😀", 'last"""\nprint(value)'];
    const colored = highlightCodeParts(parts, "python");
    expect(colored.map((tokens) => tokens.map((t) => t.text).join(""))).toEqual(parts);
    expect(colored[1].every((t) => t.classes.includes("hljs-string"))).toBe(true);
    const comments = highlightCodeParts(["/* comment", "continued */ int n = 4;"], "cpp");
    expect(comments[1][0].classes).toContain("hljs-comment");
    expect(comments[1].find((t) => t.text === "int")?.classes).toContain("hljs-type");
  });
  it("round-trips escaped characters, blank pages and leading/trailing whitespace", () => {
    const parts = [
      '  const char* s = "<script>&amp; \\" ’😀";\n',
      "",
      "\n    char x = '\\''; // $$x$$",
    ];
    expect(
      highlightCodeParts(parts, "cpp").map((tokens) =>
        tokens.map((t) => t.text).join(""),
      ),
    ).toEqual(parts);
    expect(
      highlightCodeParts(parts, "unknown").map((tokens) =>
        tokens.map((t) => t.text).join(""),
      ),
    ).toEqual(parts);
  });
});
