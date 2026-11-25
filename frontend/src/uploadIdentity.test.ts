import { describe, expect, it } from "vitest";
import { mergeUploadFiles, pdfFingerprint } from "./uploadIdentity";
import { existingImport, importIdentity } from "./importIdentity";

describe("duplicate imports", () => {
  it("merges repeated picks without dropping changed versions or distinct PDFs", () => {
    const a = new File(["one"], "paper.pdf", { lastModified: 1 });
    const copy = new File(["one"], "paper.pdf", { lastModified: 1 });
    const changed = new File(["two"], "paper.pdf", { lastModified: 2 });
    const other = new File(["one"], "other.pdf", { lastModified: 1 });
    expect(mergeUploadFiles([a], [copy, changed, other, copy])).toEqual({
      files: [a, changed, other],
      duplicates: 2,
    });
  });
  it("checks PDF content before submission, including renamed copies", async () => {
    const original = new File(["%PDF-1.7 sample"], "a.pdf");
    const renamed = new File(["%PDF-1.7 sample"], "b.pdf");
    const different = new File(["%PDF-1.7 changed"], "a.pdf");
    expect(await pdfFingerprint(original)).toMatch(/^[a-f0-9]{64}$/);
    expect(await pdfFingerprint(original)).toBe(await pdfFingerprint(renamed));
    expect(await pdfFingerprint(original)).not.toBe(await pdfFingerprint(different));
  });
  it("recognizes a new search result ID as the same source", () => {
    expect(importIdentity({ id: "first", doi: "https://doi.org/10.1234/ABC" })).toBe(
      importIdentity({ id: "new-id", doi: "10.1234/abc" }),
    );
    expect(importIdentity({ id: "first", url: "https://example.org/paper/" })).toBe(
      importIdentity({ id: "new-id", url: "https://example.org/paper" }),
    );
  });
  it("offers the existing paper without merging distinct arXiv versions", () => {
    const paper = {
      id: "saved",
      title: "Title",
      source_name: "a.pdf",
      source_url: "https://arxiv.org/abs/1234.5678v1",
    };
    expect(
      existingImport({ id: "new-id", title: "Title", url: paper.source_url }, [paper]),
    ).toBe(paper);
    expect(
      existingImport(
        { id: "v2", title: "Title", url: "https://arxiv.org/abs/1234.5678v2" },
        [paper],
      ),
    ).toBeUndefined();
  });
});
