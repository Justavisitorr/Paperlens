import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ReferenceTree, ReferenceTreeDialog } from "./ReferenceTree";
import {
  mergeReferenceBranches,
  referenceBranchStatus,
  type ReferenceTreeBranch,
} from "./referenceTreeApi";

const node = (id: string) => ({
  id,
  title: id,
  year: 2020,
  url: `https://openalex.org/${id}`,
  reference_count: 1,
});
const branch = (offset: number, ids: string[]): ReferenceTreeBranch => ({
  state: "available",
  message: "",
  node: node("W1"),
  references: ids.map(node),
  offset,
  next_offset: offset ? null : 20,
  total: 22,
  cached: true,
  checked_at: "2026-11-12",
  source: "OpenAlex",
  source_url: "https://api.openalex.org/works/W1",
});

describe("citation-tree navigation", () => {
  it("keeps previous pages, removes duplicates and replaces the whole branch on refresh", () => {
    const first = branch(0, ["W2", "W3"]);
    const next = branch(20, ["W3", "W4"]);
    expect(mergeReferenceBranches(first, next).references.map((item) => item.id)).toEqual(
      ["W2", "W3", "W4"],
    );
    expect(
      mergeReferenceBranches(first, branch(0, ["W5"])).references.map((item) => item.id),
    ).toEqual(["W5"]);
  });
  it("stops cycles before depth limits and allows continuing from a deep node", () => {
    expect(referenceBranchStatus("W2", ["W1", "W2"])).toBe("cycle");
    expect(referenceBranchStatus("W9", ["W1", "W2", "W3", "W4", "W5", "W6"])).toBe(
      "limit",
    );
    expect(referenceBranchStatus("W9", ["W6"])).toBe("expandable");
  });
  it("does not query or mount a modal before the reader chooses the tree", () => {
    const html = renderToStaticMarkup(
      <ReferenceTree paperId="p" referenceId="r" label="Citation" />,
    );
    expect(html).toContain("引用树");
    expect(html).not.toContain('role="dialog"');
  });
  it("names the modal and explains direction and incomplete coverage", () => {
    const html = renderToStaticMarkup(
      <ReferenceTreeDialog
        paperId="p"
        referenceId="r"
        label="<unsafe>"
        onClose={() => {}}
      />,
    );
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain("文献 → 它引用的文献");
    expect(html).toContain("未收录的引用不会显示");
    expect(html).toContain("&lt;unsafe&gt;");
    expect(html).not.toContain("<unsafe>");
  });
});
