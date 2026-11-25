import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReferenceSourceActions } from "./ReferenceSourceActions";
import {
  REFERENCE_SOURCE_KEY,
  displayReferenceText,
  normalizeSourcePreference,
  orderedReferenceSources,
  preferredReferenceSource,
  readSourcePreference,
  referencePreview,
  referenceSources,
  saveSourcePreference,
} from "./referenceSources";
import { formatCitation } from "./citationFormats";

const phi =
  "Authors. Phi-3 technical report. CoRR, 2024. " +
  "doi: 10.48550/ARXIV.2404.14219. URL https://doi.org/10.48550/arXiv.2404.14219.";

describe("reference destinations", () => {
  it("recognizes the same DOI in two cases as one direct source in old reports", () => {
    const sources = orderedReferenceSources(referenceSources(phi));
    expect(sources.map((source) => source.kind)).toEqual(["doi", "search"]);
    expect(preferredReferenceSource(sources)).toMatchObject({
      href: "https://doi.org/10.48550/arxiv.2404.14219",
      label: "DOI · arXiv",
    });
    const html = renderToStaticMarkup(
      <ReferenceSourceActions text={phi} preference="doi" />,
    );
    expect(html).toContain('href="https://doi.org/10.48550/arxiv.2404.14219"');
    expect(html).toContain("原文链接 ↗");
    expect(html).toContain('aria-label="选择文献来源"');
    expect(html).not.toContain("搜索原文 ↗");
    expect(formatCitation({ text: phi }, "text")).toBe(phi);
    expect(formatCitation({ text: phi }, "bibtex")).toContain(
      "doi = {10.48550/arxiv.2404.14219}",
    );
  });

  it("orders explicitly cited sources by preference and leaves the others selectable", () => {
    const text =
      "Paper. doi:10.1234/paper. arXiv:2510.14150. " +
      "URL https://publisher.example/paper.";
    const defaultOrder = orderedReferenceSources(referenceSources(text));
    expect(defaultOrder.map((source) => source.kind)).toEqual([
      "doi",
      "arxiv",
      "provided",
      "search",
    ]);
    expect(defaultOrder[2].label).toBe("文献提供的链接 · publisher.example");
    expect(
      preferredReferenceSource(orderedReferenceSources(referenceSources(text), "arxiv"))
        .href,
    ).toBe("https://arxiv.org/abs/2510.14150");
    expect(
      preferredReferenceSource(
        orderedReferenceSources(referenceSources(text), "provided"),
      ).href,
    ).toBe("https://publisher.example/paper");
  });

  it("prioritizes recognizable publisher article pages over arXiv without guessing from other hosts", () => {
    const text =
      "Paper. arXiv:2510.14150. " +
      "https://dl.acm.org/doi/10.1145/1234567. https://github.com/example/dataset.";
    const sources = orderedReferenceSources(referenceSources(text));
    expect(sources.map((source) => source.kind)).toEqual([
      "publisher",
      "arxiv",
      "provided",
      "search",
    ]);
    expect(preferredReferenceSource(sources).label).toContain(
      "出版网站链接 · dl.acm.org",
    );
    expect(preferredReferenceSource(orderedReferenceSources(sources, "arxiv")).kind).toBe(
      "arxiv",
    );
  });

  it("does not mistake the abstract and PDF of one arXiv paper for conflicting works", () => {
    const sources = orderedReferenceSources(
      referenceSources("Paper arXiv:2510.14150. https://arxiv.org/pdf/2510.14150.pdf"),
    );
    expect(sources.map((source) => source.kind)).toEqual(["arxiv", "arxiv", "search"]);
    expect(preferredReferenceSource(sources).kind).toBe("arxiv");
  });

  it("keeps conflicting identifiers as explicit choices and does not select an arbitrary one", () => {
    const sources = orderedReferenceSources(
      referenceSources("Two works: 10.1234/a and 10.1234/b."),
    );
    expect(sources.map((source) => source.kind)).toEqual(["doi", "doi", "search"]);
    expect(sources.slice(0, 2).map((source) => source.label)).toEqual([
      "DOI · 来源 1",
      "DOI · 来源 2",
    ]);
    expect(preferredReferenceSource(sources).kind).toBe("search");
  });

  it("handles TeX URL wraps and punctuation without inventing broken or unsafe destinations", () => {
    const wrapped = "A work. \\url{https://doi.org/10.48550/\n arXiv.2404.14219}.";
    expect(referenceSources(wrapped)[0].href).toBe(
      "https://doi.org/10.48550/arxiv.2404.14219",
    );
    expect(formatCitation({ text: wrapped }, "bibtex")).toContain(
      "doi = {10.48550/arxiv.2404.14219}",
    );
    expect(referenceSources("Paper (doi:10.1234/a(b)).")[0].href).toBe(
      "https://doi.org/10.1234/a(b)",
    );
    for (const text of [
      "Broken https://doi.org/10.1234/\n continuation",
      "Broken doi:10.1234/partial\n continuation",
      "Broken https://example.org/partial\n continuation",
      "Truncated https://example.org/paper...",
      "Truncated doi:10.1234/paper…",
      "Command doi:10.1234/\\input{file}",
      "Private https://user:password@example.org/paper",
    ]) {
      expect(referenceSources(text).map((source) => source.kind)).toEqual(["search"]);
    }
    expect(referenceSources("Paper", "javascript:alert(1)")[0].kind).toBe("search");
    expect(
      referenceSources(
        "Search https://search.crossref.org/?q=10.1234/not-an-identifier",
      )[0].kind,
    ).toBe("search");
  });
});

describe("reference display", () => {
  it("hides technical tails while keeping title, year and the source for copying", () => {
    expect(displayReferenceText(phi)).toBe(
      "Authors. Phi-3 technical report. CoRR, 2024.",
    );
    expect(
      displayReferenceText("  Author.  A paper. URL https://example.org/paper."),
    ).toBe("  Author.  A paper.");
    expect(displayReferenceText("A paper. \\url{https://example.org/one}")).toBe(
      "A paper.",
    );
    expect(displayReferenceText("A paper. \\href{https://example.org}{Full text}")).toBe(
      "A paper. Full text",
    );
    expect(displayReferenceText("A paper. doi:10.1234/abc. Journal, 2024.")).toBe(
      "A paper. Journal, 2024.",
    );
    expect(displayReferenceText("https://example.org/only")).toBe(
      "文献未提供其他书目信息",
    );
  });
  it("summarizes long entries while retaining their beginning and title end", () => {
    const text = "Many authors, ".repeat(90) + "A clear title. Venue, 2024.";
    const preview = referencePreview(text)!;
    expect(preview.length).toBeLessThan(420);
    expect(preview).toContain("Many authors");
    expect(preview).toContain("A clear title. Venue, 2024.");
    expect(referencePreview("Short citation.")).toBeNull();
  });
  it("uses original punctuation to keep a long Phi-3 citation compact", () => {
    const text =
      "Author One, Author Two, " +
      "Other Author, ".repeat(100) +
      "Final Author. Phi-3 technical report: A highly capable language model locally on your phone. CoRR, abs/2404.14219, 2024.";
    expect(referencePreview(text)).toBe(
      "Author One, Author Two, … Phi-3 technical report: A highly capable language model locally on your phone. CoRR, abs/2404.14219, 2024.",
    );
  });
  it("bounds unpunctuated excerpts without cutting through words", () => {
    const preview = referencePreview("UnpunctuatedAuthor ".repeat(500))!;
    expect(preview.length).toBeLessThanOrEqual(325);
    expect(preview.replace("…", "").trim().split(/\s+/u)).toEqual(
      expect.arrayContaining(["UnpunctuatedAuthor"]),
    );
    expect(
      preview
        .replace("…", "")
        .trim()
        .split(/\s+/u)
        .every((word) => word === "UnpunctuatedAuthor"),
    ).toBe(true);
    expect(referencePreview("x".repeat(5000))).toBe("…");
  });
});

describe("source preference", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("saves in the same wrapped browser storage used by other preferences", () => {
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    });
    vi.stubGlobal("window", new EventTarget());
    expect(normalizeSourcePreference("unknown")).toBe("doi");
    expect(saveSourcePreference("arxiv")).toBe(true);
    expect(values.get(REFERENCE_SOURCE_KEY)).toBe('"arxiv"');
    expect(readSourcePreference()).toBe("arxiv");
  });
});
