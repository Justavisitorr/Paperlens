import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  ImportCandidateActions,
  ImportCandidateCard,
  ImportSource,
} from "./ImportSource";
import type { ImportCandidate } from "./api";

vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});

const candidate: ImportCandidate = {
  id: "paper-1",
  title: "A real paper title",
  authors: ["First Author", "Second Author"],
  year: 2024,
  venue: "Research venue",
  abstract: "Abstract supplied by the source.",
  url: "https://example.org/paper",
  doi: "10.1234/paper",
  pdf_url: "https://example.org/paper.pdf",
};

describe("paper import confirmation", () => {
  it("shows source metadata and a choice without claiming the paper is imported", () => {
    const html = renderToStaticMarkup(
      <ImportCandidateCard
        candidate={candidate}
        selected={false}
        disabled={false}
        onSelect={() => {}}
      />,
    );
    for (const text of [
      candidate.title,
      "First Author、Second Author",
      "2024 · Research venue",
      candidate.abstract!,
      "10.1234/paper",
      "可下载",
    ])
      expect(html).toContain(text);
    expect(html).toContain('href="https://example.org/paper"');
    expect(html).not.toContain("checked=");
    expect(html).not.toContain("已导入");
  });
  it("distinguishes metadata-only results from downloadable papers", () => {
    const html = renderToStaticMarkup(
      <ImportCandidateCard
        candidate={{ ...candidate, pdf_url: undefined, abstract: "", authors: [] }}
        selected={true}
        disabled={false}
        onSelect={() => {}}
      />,
    );
    expect(html).toContain("需上传");
    expect(html).toContain("来源未提供摘要");
    expect(html).toContain("来源未提供作者");
    expect(html).not.toContain("可下载");
  });
  it("has a lookup action before any processing confirmation", () => {
    const html = renderToStaticMarkup(
      <ImportSource
        profile="economy"
        disabled={false}
        onImported={() => {}}
        onLocalUpload={() => {}}
      />,
    );
    expect(html).toContain("查找论文");
    expect(html).not.toContain('class="import-confirm"');
  });
  it("keeps processing choices and submission inside only the selected result", () => {
    const actions = (
      <ImportCandidateActions
        candidate={candidate}
        profile="detailed"
        processingOptions={<div data-testid="processing-options">三档选择</div>}
        disabled={false}
        importing={false}
        imported={false}
        error="PDF 下载失败，请重试。"
        onSubmit={() => {}}
        onLocalUpload={() => {}}
      />
    );
    for (const selected of [true, false]) {
      const html = renderToStaticMarkup(
        <ImportCandidateCard
          candidate={candidate}
          selected={selected}
          disabled={false}
          onSelect={() => {}}
        >
          {actions}
        </ImportCandidateCard>,
      );
      if (selected) {
        expect(html).toContain("三档选择");
        expect(html).toContain("开始处理");
        expect(html).toContain("PDF 下载失败，请重试。");
        expect(html.indexOf('class="import-candidate-actions"')).toBeLessThan(
          html.indexOf("开始处理"),
        );
        expect(html.indexOf("开始处理")).toBeLessThan(html.indexOf("</article>"));
      } else {
        expect(html).not.toContain("三档选择");
        expect(html).not.toContain("开始处理");
        expect(html).not.toContain("PDF 下载失败");
      }
    }
  });
  it("replaces submission with queue status after import", () => {
    const html = renderToStaticMarkup(
      <ImportCandidateActions
        candidate={candidate}
        profile="standard"
        disabled={false}
        importing={false}
        imported={true}
        error=""
        onSubmit={() => {}}
        onLocalUpload={() => {}}
      />,
    );
    expect(html).toContain("已加入队列");
    expect(html).toContain('role="status"');
    expect(html).not.toContain("<button");
  });
  it("offers local upload in place of processing for a result without a PDF", () => {
    const html = renderToStaticMarkup(
      <ImportCandidateActions
        candidate={{ ...candidate, pdf_url: undefined }}
        profile="standard"
        processingOptions={<div>三档选择</div>}
        disabled={false}
        importing={false}
        imported={false}
        error=""
        onSubmit={() => {}}
        onLocalUpload={() => {}}
      />,
    );
    expect(html).toContain("上传文件");
    expect(html).not.toContain("三档选择");
    expect(html).not.toContain("开始处理");
  });
});
