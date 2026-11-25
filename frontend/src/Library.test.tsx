import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { Library } from "./Library";
import type { PaperSummary } from "./api";

vi.mock("./PaperCover", () => ({ PaperCover: () => <span data-cover="first-page" /> }));
vi.mock("./pdfRuntime", () => ({ dataBytes: () => new Uint8Array() }));
const paper: PaperSummary = {
  id: "paper",
  title: "Real source title",
  source_name: "paper.pdf",
  authors: ["Alice Smith"],
  year: "2020",
  venue: "Original Journal",
  abstract: "Original paper abstract.",
  pages: 7,
};
function render(embedded = false) {
  return renderToStaticMarkup(
    <Library
      papers={[paper]}
      loading={false}
      error=""
      source="native"
      embedded={embedded}
      activeJobs={0}
      onUpload={() => {}}
      onRemove={() => {}}
      onRemoveMany={async () => []}
      onRetry={() => {}}
      onDownload={() => {}}
      onUpdate={async () => {}}
    />,
  );
}
describe("library entry flow", () => {
  it("list entries open details and do not directly navigate to the map", () => {
    const html = render();
    expect(html).toContain("查看《Real source title》详情");
    expect(html).toContain("Alice Smith");
    expect(html).not.toContain("#/read/paper");
    expect(html).toContain("批量管理");
  });
  it("a single offline paper has a source detail and an explicit text-reading action", () => {
    const html = render(true);
    expect(html).toContain('aria-label="论文详情"');
    expect(html).toContain("Original paper abstract.");
    expect(html).toContain('href="#/read/paper/text"');
    expect(html).toContain("阅读全文");
    expect(html).not.toContain("独立的离线报告");
    expect(html).not.toContain("批量管理");
  });
});
