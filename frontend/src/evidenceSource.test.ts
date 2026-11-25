import { describe, expect, it } from "vitest";
import { evidencePageLabel, evidencePages } from "./evidenceSource";

describe("audited evidence source pages", () => {
  it("keeps older single-page evidence readable", () => {
    expect(evidencePageLabel({ page: 4 })).toBe("PDF 第 4 页");
  });

  it("includes the starting page and retains a cross-page paragraph's range", () => {
    const evidence = { page: 4, source_pages: [5, 4, 5] };
    expect(evidencePages(evidence)).toEqual([4, 5]);
    expect(evidencePageLabel(evidence)).toBe("PDF 第 4–5 页");
  });

  it("does not invent intermediate pages or accept invalid page numbers", () => {
    expect(evidencePageLabel({ page: 2, source_pages: [7, 0, -1, 2.5] })).toBe(
      "PDF 第 2、7 页",
    );
  });
});
