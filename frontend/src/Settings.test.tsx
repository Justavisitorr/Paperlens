import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  CitationCopySettings,
  ReferenceSourceSettings,
  ReadingPreview,
  settingsGroupsForTab,
} from "./Settings";
import type { SettingsGroup } from "./api";

vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});

describe("settings organization", () => {
  it("explains the preferred source and fixed order of the remaining sources", () => {
    const html = renderToStaticMarkup(<ReferenceSourceSettings />);
    expect(html).toContain('id="reference-source-preference"');
    expect(html).toContain("DOI / 出版网站优先");
    expect(html).toContain("arXiv 优先");
    expect(html).toContain("固定次序");
  });
  it("offers citation formats with explicit BibTeX and cite usage", () => {
    const html = renderToStaticMarkup(<CitationCopySettings />);
    expect(html).toContain('id="citation-copy-format"');
    expect(html).toContain("BibTeX 文献条目");
    expect(html).toContain("LaTeX 正文引用");
    expect(html).toContain("BibTeX 与正文引用");
    expect(html).toContain("不推测缺失的作者、标题和年份");
    expect(html).toContain('value="text" selected');
  });
  it("keeps every server group reachable, including a newly added group", () => {
    const groups: SettingsGroup[] = [
      "model",
      "tiered",
      "limits",
      "files",
      "catalog",
      "auto_notes",
      "experimental",
      "future-group",
    ].map((id) => ({ id, title: id, fields: [] }));
    const reachable = (
      ["model", "processing", "catalog", "notes", "experimental"] as const
    ).flatMap((tab) => settingsGroupsForTab(groups, tab).map((group) => group.id));
    expect(reachable.sort()).toEqual(groups.filter((group) => !["experimental", "tiered"].includes(group.id)).map((group) => group.id).sort());
    expect(settingsGroupsForTab(groups, "processing").map((group) => group.id)).toContain(
      "future-group",
    );
    expect(
      settingsGroupsForTab(groups, "processing").map((group) => group.id),
    ).not.toContain("experimental");
  });
  it("previews the same size, measure, spacing and font used by the reader", () => {
    const html = renderToStaticMarkup(
      <ReadingPreview reading={{ size: 21, measure: 32, leading: 2.1, font: "sans" }} />,
    );
    expect(html).toContain("--preview-size:21px");
    expect(html).toContain("--preview-measure:32em");
    expect(html).toContain("--preview-leading:2.1");
    expect(html).toContain('data-font="sans"');
    expect(html).toContain("示例文字");
  });
});
