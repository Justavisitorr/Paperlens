import { describe, expect, it } from "vitest";
import { readingMatches, stepSearchIndex } from "./readingSearch";
import type { DashboardReport } from "./types";

const fixture = () => {
  const blocks = Array.from({ length: 5 }, (_, index) => ({
    id: `b${index}`,
    text: `Search needle paragraph ${index}`,
    section: "Methods",
  }));
  return {
    document: { blocks },
    reading: {
      source_version: "fixture",
      items: blocks.map((block, index) => ({
        id: `r${index}`,
        kind: "paragraph",
        page: index + 1,
        parts: [{ block_id: block.id, start: 0, end: block.text.length, join: "" }],
      })),
      references: [
        {
          id: "ref1",
          kind: "reference",
          page: 6,
          content: "Needle Author, citation",
          parts: [],
        },
      ],
      information: {},
    },
  } as unknown as DashboardReport;
};

describe("shared reader search navigation", () => {
  it("keeps clicks on later results and next/previous navigation in the same sequence", () => {
    const matches = readingMatches(fixture(), "needle");
    let index = matches.findIndex((match) => match.id === "r3");
    expect(index).toBe(3);
    index = stepSearchIndex(index, matches.length, 1);
    expect(matches[index].id).toBe("r4");
    index = stepSearchIndex(index, matches.length, -1);
    expect(matches[index].id).toBe("r3");
    expect(matches[stepSearchIndex(-1, matches.length, 1)].id).toBe("r0");
    expect(matches[stepSearchIndex(-1, matches.length, -1)].id).toBe("ref1");
    expect(stepSearchIndex(0, matches.length, -1)).toBe(matches.length - 1);
    expect(stepSearchIndex(matches.length - 1, matches.length, 1)).toBe(0);
  });
  it("searches visible projected content and references instead of stale raw blocks", () => {
    const report = fixture();
    report.reading!.items[0].content = "Corrected reading: transformed-term";
    expect(readingMatches(report, "transformed-term").map((match) => match.id)).toEqual([
      "r0",
    ]);
    expect(readingMatches(report, "Needle").map((match) => match.id)).toEqual([
      "r1",
      "r2",
      "r3",
      "r4",
      "ref1",
    ]);
    expect(readingMatches(report, "Author")[0].section).toBe("参考文献");
  });
  it("has no selected hit or navigation target for an empty result set", () => {
    expect(readingMatches(fixture(), "   ")).toEqual([]);
    expect(readingMatches(fixture(), "missing")).toEqual([]);
    expect(stepSearchIndex(0, 0, 1)).toBe(-1);
  });
});
