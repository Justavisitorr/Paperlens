import { afterEach, describe, expect, it, vi } from "vitest";
import {
  libraryPapers,
  paperProgress,
  paperTitle,
  readPaperSummary,
} from "./libraryMetadata";
import { positionKey } from "./readingExperience";
import type { PaperSummary } from "./api";
import type { DashboardReport } from "./types";

const record = (id: string, values: Partial<PaperSummary> = {}): PaperSummary => ({
  id,
  title: id,
  source_name: `${id}.pdf`,
  ...values,
});
function report(): DashboardReport {
  return {
    document: {
      title: "abcdefabcdefabcdefabcdefabcdefab",
      source_name: "source.pdf",
      pages: [{ number: 1 }],
      blocks: [],
    },
    analysis: {
      title: "AI-produced reading guide",
      summary: "AI summary must never be a paper abstract",
      paper_type: "other",
      nodes: [],
    },
    evidence: [],
    previews: [{ page: 2, data_url: "data:image/png;base64,second-page" }],
    source_pdf: { data_url: "data:application/pdf;base64,source", sha256: "source" },
    reading: {
      source_version: "source",
      items: [
        {
          id: "body",
          kind: "paragraph",
          content: "摘要：The actual abstract.",
          parts: [],
        },
      ],
      references: [],
      information_items: {
        titles: [
          { content: "真实论文标题", parts: [] },
          { content: "Original alternate title", parts: [] },
        ],
        authors: [{ page: 1, content: "Alice Smith, Bob Lee (University)", parts: [] }],
        citation: [
          {
            content: "Smith A. Real paper[J]. Original Journal, 2020, 2: 1-5.",
            parts: [],
          },
        ],
        publication: [{ content: "DOI: 10.1234/source.", parts: [] }],
      },
    },
    run: { created_at: "2026-01-01T00:00:00Z", model: "model", status: "complete" },
  } as unknown as DashboardReport;
}
afterEach(() => vi.unstubAllGlobals());

describe("original publication metadata", () => {
  it("uses source titles, authors, venue and actual abstract, without analysis text", () => {
    const summary = readPaperSummary(report(), "paper");
    expect(summary.title).toBe("真实论文标题");
    expect(summary.original_title).toBe("真实论文标题");
    expect(summary.alternate_title).toBe("Original alternate title");
    expect(summary.authors).toEqual(["Alice Smith", "Bob Lee"]);
    expect(summary.venue).toBe("Original Journal");
    expect(summary.year).toBe("2020");
    expect(summary.abstract).toBe("The actual abstract.");
    expect(summary.doi).toBe("10.1234/source");
    expect(summary.pdf_url).toBe("data:application/pdf;base64,source");
    expect(JSON.stringify(summary)).not.toContain("AI-produced");
    expect(JSON.stringify(summary)).not.toContain("AI summary");
  });
  it("never uses a second-page preview as a cover", () => {
    expect(readPaperSummary(report()).thumbnail_url).toBe("");
    const value = report();
    value.previews.push({ page: 1, data_url: "first-page", width: 200, height: 300 });
    expect(readPaperSummary(value).thumbnail_url).toBe("first-page");
  });
  it("leaves missing abstracts empty instead of substituting the AI summary", () => {
    const value = report();
    value.reading!.items[0].content = "Introduction to the method.";
    expect(readPaperSummary(value).abstract).toBe("");
  });
  it("honors an explicit user title while keeping the original separately", () => {
    expect(
      paperTitle(
        record("paper", { title: "My reading name", original_title: "Source title" }),
      ),
    ).toBe("My reading name");
  });
});

describe("library filtering and sorting", () => {
  const papers = [
    record("a", {
      authors: ["Alice"],
      reading_status: "reading",
      starred: true,
      year: "2020",
      last_opened: "2026-02-01",
      created_at: "2026-01-01",
    }),
    record("b", {
      abstract: "factorization machine",
      reading_status: "read",
      year: "2024",
      created_at: "2026-01-02",
    }),
    record("c"),
  ];
  it("searches authors and original abstracts", () => {
    expect(libraryPapers(papers, "all", " ALICE ", "title").map((p) => p.id)).toEqual([
      "a",
    ]);
    expect(
      libraryPapers(papers, "all", "factorization", "title").map((p) => p.id),
    ).toEqual(["b"]);
  });
  it("filters all reading states and stars without mutating the library", () => {
    expect(libraryPapers(papers, "reading", "", "title").map((p) => p.id)).toEqual(["a"]);
    expect(libraryPapers(papers, "unread", "", "title").map((p) => p.id)).toEqual(["c"]);
    expect(libraryPapers(papers, "read", "", "title").map((p) => p.id)).toEqual(["b"]);
    expect(libraryPapers(papers, "starred", "", "title").map((p) => p.id)).toEqual(["a"]);
    expect(papers.map((p) => p.id)).toEqual(["a", "b", "c"]);
  });
  it("sorts recent reading, creation, year and title independently", () => {
    expect(libraryPapers(papers, "all", "", "opened").map((p) => p.id)).toEqual([
      "a",
      "b",
      "c",
    ]);
    expect(libraryPapers(papers, "all", "", "added").map((p) => p.id)).toEqual([
      "b",
      "a",
      "c",
    ]);
    expect(libraryPapers(papers, "all", "", "year").map((p) => p.id)).toEqual([
      "b",
      "a",
      "c",
    ]);
  });
});

describe("existing reading positions", () => {
  it("reuses the reader source-version key and rejects stale item ids", () => {
    const getItem = vi.fn(() => JSON.stringify({ id: "body", fraction: 0.5 }));
    vi.stubGlobal("localStorage", { getItem });
    const paper = record("paper", {
      reading_source_version: "version",
      reading_item_ids: ["article-opening", "body", "article-information"],
    });
    expect(paperProgress(paper)).toBe(0.5);
    expect(getItem).toHaveBeenCalledWith(positionKey("version"));
    getItem.mockReturnValue(JSON.stringify({ id: "old-body", fraction: 0.5 }));
    expect(paperProgress(paper)).toBeNull();
  });
});
