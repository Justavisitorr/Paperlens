import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Annotation } from "./api";
import type { DashboardReport } from "./types";

const api = vi.hoisted(() => ({
  createAnnotation: vi.fn(),
  deleteAnnotation: vi.fn(),
  listAnnotations: vi.fn(),
  updateAnnotation: vi.fn(),
}));
vi.mock("./api", () => api);
import {
  annotationsMarkdown,
  editAnnotation,
  loadAnnotations,
  readAnnotationDraft,
  saveAnnotationDraft,
  segments,
} from "./annotations";

const mark = (id: string, block_id: string, start = 0): Annotation => ({
  id,
  selection: { block_id, start, end: start + 2, text: "😀" },
  exact: "😀",
  note: "检查假设\n与结论",
  page: 2,
  section: "方法",
  created_at: "2026-10-01",
  color: "blue",
  style: "underline",
  source_version: "source-v1",
  document_sha256: "abc",
});

beforeEach(() => {
  vi.clearAllMocks();
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
});

describe("annotation editing and source export", () => {
  it("updates the existing ID without recreating or deleting and keeps the prior saved note on failure", async () => {
    const old = mark("one", "b1");
    api.listAnnotations.mockResolvedValue([old]);
    await loadAnnotations("edit-success");
    api.updateAnnotation.mockResolvedValue({ ...old, note: "新笔记", color: "green" });
    await editAnnotation("edit-success", old, { note: "新笔记", color: "green" });
    expect(api.updateAnnotation).toHaveBeenCalledWith("edit-success", "one", {
      note: "新笔记",
      color: "green",
    });
    expect((await loadAnnotations("edit-success"))[0].note).toBe("新笔记");
    api.updateAnnotation.mockRejectedValue(new Error("offline"));
    await expect(
      editAnnotation("edit-success", old, { note: "失败草稿" }),
    ).rejects.toThrow("offline");
    expect((await loadAnnotations("edit-success"))[0].note).toBe("新笔记");
    expect(api.createAnnotation).not.toHaveBeenCalled();
    expect(api.deleteAnnotation).not.toHaveBeenCalled();
  });

  it("exports source order rather than alphabetic IDs, keeps offsets, quote, note and version", () => {
    const report = {
      analysis: { title: "论文" },
      document: { source_name: "original.pdf", blocks: [{ id: "z" }, { id: "a" }] },
    } as DashboardReport;
    const text = annotationsMarkdown(
      [mark("last", "a"), mark("second", "z", 4), mark("first", "z")],
      report,
    );
    expect(text.indexOf("UTF-16 [0, 2)")).toBeLessThan(text.indexOf("UTF-16 [4, 6)"));
    expect(text.indexOf("z，UTF-16 [4, 6)")).toBeLessThan(
      text.indexOf("a，UTF-16 [0, 2)"),
    );
    for (const source of [
      "original.pdf",
      "> 😀",
      "检查假设\n与结论",
      "source-v1",
      "SHA-256：abc",
      "蓝色 · 下划线",
    ])
      expect(text).toContain(source);
  });

  it("persists unsaved note and appearance per paper and can clear after confirmation", () => {
    const draft = {
      note: "未保存的 😀 笔记",
      color: "pink",
      style: "highlight",
    } as const;
    expect(saveAnnotationDraft("paper", "a", draft)).toBe(true);
    expect(readAnnotationDraft("paper", "a")).toEqual(draft);
    expect(readAnnotationDraft("other", "a")).toBeNull();
    saveAnnotationDraft("paper", "a", null);
    expect(readAnnotationDraft("paper", "a")).toBeNull();
    vi.stubGlobal("localStorage", {
      setItem: () => {
        throw new Error("quota");
      },
    });
    expect(saveAnnotationDraft("paper", "a", draft)).toBe(false);
  });

  it("keeps CRLF, CR and blank source lines in one exported quote", () => {
    const text = annotationsMarkdown([
      { ...mark("multiline", "b"), exact: "First\r\n\r\n## Second\rThird" },
    ]);
    expect(text).toContain("> First\n> \n> ## Second\n> Third");
    expect(text).not.toContain("\r");
  });

  it("search/highlight segmentation preserves UTF16 text exactly across emoji and overlaps", () => {
    const text = "前😀后原文";
    const result = segments(text, "😀后", [
      { id: "blue", start: 1, end: 4 },
      { id: "pink", start: 3, end: 6 },
    ]);
    expect(result.map((s) => s.text).join("")).toBe(text);
    expect(result.find((s) => s.text === "😀后")?.mark).toBe("blue");
    expect(result.find((s) => s.text === "原文")?.mark).toBe("pink");
  });
});
