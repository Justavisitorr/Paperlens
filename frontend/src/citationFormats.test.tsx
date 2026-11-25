import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CITATION_FORMAT_KEY,
  citationKey,
  copyCitation,
  escapeBibtexText,
  formatCitation,
  hasCitationText,
  normalizeCitationFormat,
  readCitationFormat,
  saveCitationFormat,
} from "./citationFormats";
import { CitationCopyButton } from "./CitationCopyButton";

describe("citation formats", () => {
  const reference = { text: "[7] 王晓明, A. Smith. A&B: 100% efficient_model #1. 2025." };

  it("keeps ordinary text intact and uses one stable key for BibTeX and cite", () => {
    expect(formatCitation(reference, "text")).toBe(reference.text);
    const key = citationKey(reference);
    expect(key).toMatch(/^ref_[a-f0-9]{16}$/);
    expect(
      citationKey({
        text: reference.text.replace("[7]", "[18]").replace("A&B:", "A&B:\n"),
      }),
    ).toBe(key);
    expect(
      citationKey({ text: reference.text.replace("efficient_model", "different_model") }),
    ).not.toBe(key);
    const bibtex = formatCitation(reference, "bibtex");
    const cite = formatCitation(reference, "latex");
    expect(bibtex).toContain(`@misc{${key},`);
    expect(bibtex).toContain("note = {王晓明, A. Smith.");
    expect(bibtex).not.toMatch(/\b(author|title|year)\s*=/);
    expect(cite).toBe(`\\cite{${key}}`);
    expect(formatCitation(reference, "bibtex_latex")).toBe(`${bibtex}\n\n${cite}`);
  });

  it("escapes literal TeX syntax, special characters and Chinese prose without executing commands", () => {
    const text = "中文 & % _ # $ {x} \\input{file} ^ ~";
    expect(escapeBibtexText(text)).toBe(
      "中文 \\& \\% \\_ \\# \\$ \\{x\\} \\textbackslash{}input\\{file\\} \\textasciicircum{} \\textasciitilde{}",
    );
    expect(formatCitation({ text }, "bibtex")).not.toContain("\\input");
  });

  it("uses explicit DOI identity across differently written citations and leaves verbatim fields unescaped", () => {
    const first = { text: "An article. doi:10.1234/AB_cd. 2025." };
    const second = {
      text: "Another representation of the same article.",
      href: "https://doi.org/10.1234/ab_cd",
    };
    expect(citationKey(first)).toBe(citationKey(second));
    const bibtex = formatCitation(first, "bibtex");
    expect(bibtex).toContain("doi = {10.1234/ab_cd}");
    expect(bibtex).toContain("url = {https://doi.org/10.1234/ab_cd}");
    expect(bibtex).not.toContain("doi = {10.1234/ab\\_cd}");
  });

  it("preserves URL query and fragment semantics while encoding brace delimiters", () => {
    const bibtex = formatCitation(
      {
        text: "A public dataset.",
        href: "https://example.org/a_b?label={x}&percent=25%25#section",
      },
      "bibtex",
    );
    expect(bibtex).toContain(
      "url = {https://example.org/a_b?label=%7Bx%7D&percent=25%25#section}",
    );
    expect(bibtex).not.toContain("\\_");
  });

  it("does not turn unsafe/search links or ambiguous identifiers into a claimed DOI", () => {
    for (const href of [
      "javascript:alert(1)",
      "https://user:password@example.org/private",
      "https://scholar.google.com/scholar?q=paper",
    ]) {
      expect(formatCitation({ text: "A cited paper.", href }, "bibtex")).not.toContain(
        "url =",
      );
    }
    expect(
      formatCitation({ text: "Two records: 10.1234/one and 10.1234/two" }, "bibtex"),
    ).not.toContain("doi =");
    expect(
      formatCitation({ text: "Literal DOI command: 10.1234/\\input{file}" }, "bibtex"),
    ).not.toContain("doi =");
  });

  it("rejects empty rows, separators and index-only placeholders in every format", () => {
    for (const text of ["", " \n ", "-", "—", "[23]", "[23] -", "2025"]) {
      expect(hasCitationText(text)).toBe(false);
      expect(() => formatCitation({ text }, "bibtex")).toThrow("没有可复制");
    }
    expect(hasCitationText("文献标题")).toBe(true);
    expect(hasCitationText("10.1234/5678")).toBe(true);
    expect(renderToStaticMarkup(<CitationCopyButton text="-" />)).toContain("disabled");
  });
});

describe("citation copy preference and clipboard", () => {
  let values: Map<string, string>;
  let storage: { getItem: ReturnType<typeof vi.fn>; setItem: ReturnType<typeof vi.fn> };
  beforeEach(() => {
    values = new Map();
    storage = {
      getItem: vi.fn((key: string) => values.get(key) ?? null),
      setItem: vi.fn((key: string, value: string) => values.set(key, value)),
    };
    vi.stubGlobal("localStorage", storage);
    vi.stubGlobal("window", new EventTarget());
    saveCitationFormat("text");
  });
  afterEach(() => vi.unstubAllGlobals());

  it("defaults unknown or damaged preferences to plain text", () => {
    expect(normalizeCitationFormat("unsupported")).toBe("text");
    values.set(CITATION_FORMAT_KEY, '"unsupported"');
    expect(readCitationFormat()).toBe("text");
    values.set(CITATION_FORMAT_KEY, "invalid json");
    expect(readCitationFormat()).toBe("text");
  });

  it("saves the chosen format and notifies mounted copy controls immediately", () => {
    const onChange = vi.fn();
    window.addEventListener("paperlens:citation-copy-format-changed", onChange);
    expect(saveCitationFormat("bibtex")).toBe(true);
    expect(readCitationFormat()).toBe("bibtex");
    expect(values.get(CITATION_FORMAT_KEY)).toBe('"bibtex"');
    expect(onChange).toHaveBeenCalledOnce();
  });

  it("keeps an unsaved choice active when writes fail but the older preference remains readable", () => {
    storage.setItem.mockImplementationOnce(() => {
      throw new Error("quota exceeded");
    });
    expect(saveCitationFormat("latex")).toBe(false);
    expect(values.get(CITATION_FORMAT_KEY)).toBe('"text"');
    expect(readCitationFormat()).toBe("latex");
    expect(saveCitationFormat("bibtex")).toBe(true);
    expect(readCitationFormat()).toBe("bibtex");
  });

  it("copies the selected format and reports both clipboard absence and rejected writes", async () => {
    const reference = { text: "An original citation. 2025." };
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await expect(copyCitation(reference, "latex")).resolves.toBe(
      formatCitation(reference, "latex"),
    );
    expect(writeText).toHaveBeenCalledWith(formatCitation(reference, "latex"));
    writeText.mockRejectedValueOnce(new Error("Permission denied"));
    await expect(copyCitation(reference, "bibtex")).rejects.toThrow("Permission denied");
    vi.stubGlobal("navigator", {});
    await expect(copyCitation(reference, "bibtex")).rejects.toThrow("手动复制");
  });
});
