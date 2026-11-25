import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PdfSourceProps } from "./PdfSource";
import type { DashboardReport, Evidence } from "./types";

const source = vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
  return vi.fn();
});
vi.mock("./PdfSource", () => ({
  PdfSource: (props: PdfSourceProps) => {
    source(props);
    return <div data-testid="original-pdf" data-page={props.page} />;
  },
}));

import { EvidenceCard } from "./EvidencePanel";

const evidence: Evidence = {
  id: "claim-source",
  block_id: "paragraph",
  page: 1,
  page_label: "1",
  section: "Results",
  quote: "A paragraph checked against the original paper.",
  bbox: { x0: 0.1, y0: 0.2, x1: 0.8, y1: 0.3 },
  match: "exact",
};
const report = (withPdf = false, withPreview = true): DashboardReport =>
  ({
    document: { parser: "pymupdf", blocks: [] },
    previews: withPreview
      ? [{ page: 1, width: 600, height: 800, data_url: "data:image/png;base64,aW1hZ2U=" }]
      : [],
    source_pdf: withPdf
      ? { data_url: "/api/library/paper-a/pdf", sha256: "paper-a" }
      : null,
  }) as unknown as DashboardReport;

beforeEach(() => source.mockClear());

describe("evidence source presentation", () => {
  it("shows possible source pages and a conservative explanation instead of a precise cross-page highlight", () => {
    const html = renderToStaticMarkup(
      <EvidenceCard
        evidence={{ ...evidence, source_pages: [1, 2] }}
        report={report()}
        expanded
      />,
    );
    expect(html).toContain("PDF 第 1–2 页");
    expect(html).toContain("来源段落跨越这些页面，摘录的具体页内位置尚未单独核实。");
    expect(html).toMatch(
      /<button[^>]*aria-pressed="true"[^>]*>查看 PDF 第 1 页<\/button>/,
    );
    expect(html).toMatch(
      /<button[^>]*aria-pressed="false"[^>]*>查看 PDF 第 2 页<\/button>/,
    );
    expect(html).toContain("请对照原页核查");
    expect(html).not.toContain("evidence-highlight");
    expect(html).not.toContain("黄色区域为引用段落");
    expect(source).not.toHaveBeenCalled();
  });

  it("opens a cross-page original PDF without passing an invented single-page box", () => {
    const value = report(true);
    const html = renderToStaticMarkup(
      <EvidenceCard
        evidence={{ ...evidence, source_pages: [1, 2] }}
        report={value}
        expanded
      />,
    );
    expect(html).toContain('data-testid="original-pdf"');
    expect(html).not.toContain("evidence-highlight");
    expect(html).not.toContain('class="page-preview"');
    expect(source).toHaveBeenCalledExactlyOnceWith({
      report: value,
      page: 1,
      onNavigate: expect.any(Function),
    });
    expect(source.mock.calls[0][0]).not.toHaveProperty("box");
  });

  it("retains the precise preview highlight for older single-page evidence", () => {
    const html = renderToStaticMarkup(
      <EvidenceCard evidence={evidence} report={report(true)} expanded />,
    );
    expect(html).toContain('class="page-badge">P1</span>');
    expect(html).toContain('class="evidence-highlight"');
    expect(html).toContain("黄色区域为引用段落");
    expect(html).toContain("位置高亮为原文段落范围");
    expect(html).not.toContain("证据来源页");
    expect(source).not.toHaveBeenCalled();
  });

  it("opens an available original PDF when the single-page preview is absent", () => {
    const value = report(true, false);
    const html = renderToStaticMarkup(
      <EvidenceCard evidence={evidence} report={value} expanded />,
    );
    expect(html).toContain('data-testid="original-pdf" data-page="1"');
    expect(html).not.toContain("preview-unavailable");
    expect(html).not.toContain("evidence-highlight");
    expect(source).toHaveBeenCalledExactlyOnceWith({
      report: value,
      page: 1,
      onNavigate: expect.any(Function),
    });
  });

  it("does not open PDF resources while the evidence source is collapsed", () => {
    const html = renderToStaticMarkup(
      <EvidenceCard
        evidence={{ ...evidence, source_pages: [1, 2] }}
        report={report(true, false)}
      />,
    );
    expect(html).toContain("PDF 第 1–2 页");
    expect(html).toContain('aria-expanded="false"');
    expect(source).not.toHaveBeenCalled();
  });
});
