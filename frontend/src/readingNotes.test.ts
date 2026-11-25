import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DashboardReport, ReadingItem } from "./types";
import {
  checkReadingNote,
  createReadingNotesStore,
  readingNoteAnchor,
  readingNotesKey,
  readingNotesMarkdown,
  type ReadingNote,
  annotationNotes,
} from "./readingNotes";

const item = (
  id: string,
  content: string,
  kind: ReadingItem["kind"] = "paragraph",
): ReadingItem => ({
  id,
  content,
  kind,
  parts: [],
  page: 2,
  label: "",
  level: 1,
  asset: "",
  result_ids: [],
  visual_ids: [],
});
const fixture = (): DashboardReport =>
  ({
    document: { sha256: "source-sha", source_name: "source.pdf", blocks: [] },
    run: { id: "run-1" },
    analysis: { title: "测试论文" },
    reading: {
      version: "3",
      source_version: "version-1",
      items: [item("z", "来源😀文本"), item("a", "后一段")],
      references: [item("ref", "Reference text", "reference")],
      information: {},
      information_items: { authors: [item("author", "作者信息")] },
      hidden_ids: [],
      warnings: [],
    },
  }) as unknown as DashboardReport;
const remoteNote = (note: ReadingNote) => ({ ...note, updated_at: "server-time" });
it("bridges original highlights into the notebook without modifying either store", () => {
  const r = fixture();
  r.document.blocks = [
    { id: "b", text: "text to highlight", page: 2 },
  ] as DashboardReport["document"]["blocks"];
  r.reading!.items[0].parts = [{ block_id: "b", start: 0, end: 17, join: "" }];
  const annotation = {
    id: "a".repeat(32),
    selection: { block_id: "b", start: 8, end: 17, text: "highlight" },
    exact: "highlight",
    note: "my note",
    page: 2,
    created_at: "now",
    color: "pink" as const,
    style: "underline" as const,
  };
  const snapshot = JSON.stringify(annotation);
  const [note] = annotationNotes([annotation], r);
  expect(note.item_id).toBe("z");
  expect(note.annotation_id).toBe(annotation.id);
  expect(note.text).toBe("my note");
  expect(note.mark?.color).toBe("pink");
  expect(note.detached).toBe(false);
  expect(JSON.stringify(annotation)).toBe(snapshot);
  expect(
    annotationNotes(
      [{ ...annotation, selection: { ...annotation.selection, text: "changed" } }],
      r,
    )[0].detached,
  ).toBe(true);
});
const response = (body: unknown, ok = true) => ({
  ok,
  status: ok ? 200 : 503,
  json: async () => body,
});
let values: Map<string, string>;
beforeEach(() => {
  values = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) || null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
  vi.stubGlobal("fetch", vi.fn());
});

describe("local durable reading notes", () => {
  it("keeps paragraph marks offline, syncs their exact appearance and retains them while editing notes", async () => {
    const report = fixture();
    const store = createReadingNotesStore("marks", report, false);
    const mark = {
      surface: "original" as const,
      text: "来源😀文本",
      start: 2,
      end: 6,
      color: "blue" as const,
      style: "underline" as const,
    };
    const saved = await store.save({
      item_id: "z",
      kind: "note",
      text: "",
      quote: "😀文本",
      mark,
    });
    expect(fetch).not.toHaveBeenCalled();
    const restored = createReadingNotesStore("marks", report, true);
    vi.mocked(fetch).mockResolvedValue(response({ note: saved }) as Response);
    await restored.retrySync();
    expect(JSON.parse(vi.mocked(fetch).mock.calls[0][1]!.body as string).mark).toEqual(
      mark,
    );
    expect(restored.getSnapshot().items[0].mark).toEqual(mark);
    expect(readingNotesMarkdown([saved], report)).toContain("下划线");
  });
  it("preserves generated note types through save, reload and Markdown export", async () => {
    const report = fixture();
    const store = createReadingNotesStore("paper", report, false);
    await store.save({
      item_id: "z",
      kind: "ai",
      text: "概要",
      auto_types: ["summary", "explain"],
    });
    const restored = createReadingNotesStore("paper", report, false).getSnapshot().items;
    expect(restored[0].auto_types).toEqual(["summary", "explain"]);
    expect(readingNotesMarkdown(restored, report)).toContain("AI 阅读笔记");
  });
  it("writes offline without API calls, reloads questions and isolates document/paper identity", async () => {
    const report = fixture(),
      store = createReadingNotesStore("paper", report, false);
    const note = await store.save({
      item_id: "z",
      kind: "question",
      text: "为什么？",
      quote: "😀",
    });
    expect(note.id).toMatch(/^[0-9a-f]{32}$/);
    expect(fetch).not.toHaveBeenCalled();
    expect(store.getSnapshot().pending).toBe(0);
    expect(createReadingNotesStore("paper", report, false).getSnapshot().items).toEqual([
      note,
    ]);
    expect(createReadingNotesStore("other", report, false).getSnapshot().items).toEqual(
      [],
    );
    const different = { ...report, document: { ...report.document, sha256: "another" } };
    expect(readingNotesKey("paper", different)).not.toBe(
      readingNotesKey("paper", report),
    );
  });

  it("retains an acknowledged local save on service failure and resumes its outbox after reload", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("服务离线"));
    const report = fixture(),
      store = createReadingNotesStore("paper", report, true);
    const note = await store.save({ item_id: "z", kind: "note", text: "我的理解" });
    expect(store.getSnapshot()).toMatchObject({ pending: 1, items: [note] });
    expect(store.getSnapshot().error).toContain("已保存在本浏览器");
    const reloaded = createReadingNotesStore("paper", report, true);
    vi.mocked(fetch).mockResolvedValue(response(remoteNote(note)) as Response);
    await reloaded.retrySync();
    expect(reloaded.getSnapshot().pending).toBe(0);
    expect(reloaded.getSnapshot().error).toBe("");
    expect(reloaded.getSnapshot().items[0].updated_at).toBe("server-time");
  });

  it("pins a completed chat answer using its stable ID and updates it without duplicates", async () => {
    const store = createReadingNotesStore("paper", fixture(), false);
    const id = "a".repeat(32);
    await store.save({ item_id: "z", kind: "ai", text: "第一次回答" }, id);
    await store.save({ item_id: "z", kind: "ai", text: "同条回答更新" }, id);
    expect(store.getSnapshot().items).toHaveLength(1);
    expect(store.getSnapshot().items[0]).toMatchObject({ id, text: "同条回答更新" });
    await expect(
      store.save({ item_id: "z", kind: "ai", text: "回答" }, "bad-id"),
    ).rejects.toThrow("编号无效");
  });

  it("rejects oversized questions without truncating them while retaining the longer note limit", async () => {
    const store = createReadingNotesStore("paper", fixture(), false);
    await expect(
      store.save({ item_id: "z", kind: "question", text: "问".repeat(4001) }),
    ).rejects.toThrow("4000");
    await expect(
      store.save({ item_id: "z", kind: "note", text: "记".repeat(4001) }),
    ).resolves.toHaveProperty("text", "记".repeat(4001));
  });

  it("keeps a newer edit while the previous request is in flight", async () => {
    let finish!: (r: Response) => void;
    vi.mocked(fetch).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const report = fixture(),
      store = createReadingNotesStore("paper", report, true);
    const firstSave = store.save({ item_id: "z", kind: "note", text: "旧文字" });
    const old = store.getSnapshot().items[0];
    await Promise.resolve();
    const secondSave = store.save({ item_id: "z", kind: "note", text: "新文字" }, old.id);
    vi.mocked(fetch).mockResolvedValue(
      response(remoteNote({ ...old, text: "新文字" })) as Response,
    );
    finish(response(remoteNote(old)) as Response);
    await Promise.all([firstSave, secondSave]);
    expect(store.getSnapshot().items[0].text).toBe("新文字");
    expect(store.getSnapshot().pending).toBe(0);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("refresh overlays pending edits and deletes onto remote records", async () => {
    const report = fixture(),
      local = createReadingNotesStore("paper", report, false);
    const parent = await local.save({
      item_id: "z",
      kind: "question",
      text: "保留的问题",
    });
    const child = await local.save({
      item_id: "z",
      kind: "ai",
      text: "回答",
      parent_id: parent.id,
    });
    await local.remove(parent.id);
    const store = createReadingNotesStore("paper", report, true);
    vi.mocked(fetch)
      .mockResolvedValueOnce(response({ notes: [parent, child] }) as Response)
      .mockRejectedValue(new Error("离线"));
    await store.refresh();
    expect(store.getSnapshot().items).toEqual([]);
    expect(store.getSnapshot().pending).toBeGreaterThan(0);
  });

  it("a storage write failure does not pretend the note was saved or send a request", async () => {
    const store = createReadingNotesStore("paper", fixture(), true);
    vi.stubGlobal("localStorage", {
      getItem: () => null,
      setItem: () => {
        throw new Error("quota");
      },
    });
    await expect(
      store.save({ item_id: "z", kind: "note", text: "重要内容" }),
    ).rejects.toThrow("尚未保存");
    expect(store.getSnapshot().items).toEqual([]);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not reanchor an offline pending note when the edition changes, while other notes can sync", async () => {
    const report = fixture(),
      store = createReadingNotesStore("paper", report, false);
    await store.save({ item_id: "z", kind: "note", text: "旧版笔记" });
    const changed = fixture();
    changed.reading!.source_version = "version-2";
    store.updateContext(changed, true);
    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      const input = JSON.parse(init?.body as string);
      const current = store.getSnapshot().items.find((n) => n.text === input.text)!;
      return response(current) as Response;
    });
    await store.save({ item_id: "a", kind: "note", text: "新版笔记" });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot().pending).toBe(1);
    expect(store.getSnapshot().error).toContain("原文定位已变化");
    expect(store.getSnapshot().items.find((n) => n.text === "旧版笔记")?.detached).toBe(
      true,
    );
    await store.retrySync();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("preserves anchors on editing and detaches changed text/version instead of inventing offsets", async () => {
    const report = fixture(),
      store = createReadingNotesStore("paper", report, false);
    const note = await store.save({ item_id: "z", kind: "note", text: "理解" });
    const changed = fixture();
    changed.reading!.items[0].content = "来源已修改";
    store.updateContext(changed, false);
    expect(store.getSnapshot().items[0].detached).toBe(true);
    const edited = await store.save(
      { item_id: "z", kind: "note", text: "新理解" },
      note.id,
    );
    expect(edited.anchor_text).toBe(note.anchor_text);
    expect(edited.detached).toBe(true);
    await expect(
      store.save({ item_id: "missing", kind: "note", text: "理解" }),
    ).rejects.toThrow("不存在");
    await expect(
      store.save({ item_id: "a", kind: "note", text: "理解" }, note.id),
    ).rejects.toThrow("定位");
    const versionChanged = fixture();
    versionChanged.reading!.source_version = "new";
    expect(checkReadingNote(note, versionChanged).detached).toBe(true);
  });

  it("anchors tables with the entire grid and includes reference/information items", async () => {
    const report = fixture();
    const table = item("table", "Caption", "table");
    table.table = {
      rows: 1,
      cols: 1,
      cells: [
        {
          row_start: 0,
          row_end: 1,
          col_start: 0,
          col_end: 1,
          text: "123",
          column_header: false,
        },
      ],
    };
    report.reading!.items.push(table);
    const store = createReadingNotesStore("paper", report, false);
    const note = await store.save({ item_id: "table", kind: "note", text: "数字" });
    expect(note.anchor_text).toContain('"text":"123"');
    table.table!.cells[0].text = "124";
    expect(readingNoteAnchor(table, report)).not.toBe(note.anchor_text);
    expect(checkReadingNote(note, report).detached).toBe(true);
    await expect(
      store.save({ item_id: "ref", kind: "note", text: "参考文献" }),
    ).resolves.toHaveProperty("item_id", "ref");
    await expect(
      store.save({ item_id: "author", kind: "note", text: "作者" }),
    ).resolves.toHaveProperty("item_id", "author");
  });

  it("matches server semantic table anchors and ignores floating-point geometry", async () => {
    const report = fixture();
    const table = item("table", "Caption", "table");
    const grid = {
      rows: 1,
      cols: 1,
      cells: [
        {
          row_start: 0,
          row_end: 1,
          col_start: 0,
          col_end: 1,
          text: "123",
          column_header: false,
          bbox: { x0: 0.0, y0: 1e-7, x1: 1.0, y1: 0.5 },
        },
      ],
    };
    table.table = grid;
    report.reading!.items.push(table);
    const expected =
      'Caption\n{"rows":1,"cols":1,"cells":[{"text":"123","row_start":0,"row_end":1,"col_start":0,"col_end":1,"column_header":false}]}';
    const store = createReadingNotesStore("paper", report, true);
    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      const input = JSON.parse(init?.body as string);
      const local = store.getSnapshot().items.find((n) => n.text === input.text)!;
      return response({ ...local, anchor_text: expected, detached: false }) as Response;
    });
    const saved = await store.save({ item_id: "table", kind: "note", text: "表格理解" });
    expect(saved.anchor_text).toBe(expected);
    expect(saved.detached).toBe(false);
    grid.cells[0].bbox = { x0: 0.25, y0: 0.3, x1: 0.9, y1: 0.8 };
    expect(readingNoteAnchor(table, report)).toBe(expected);
    expect(checkReadingNote(saved, report).detached).toBe(false);
    grid.cells[0].col_end = 2;
    expect(checkReadingNote(saved, report).detached).toBe(true);
  });

  it("exports by source order, keeps children after their parent, and excludes AI on request", async () => {
    const report = fixture(),
      store = createReadingNotesStore("paper", report, false);
    await store.save({ item_id: "a", kind: "note", text: "后一段笔记" });
    const parent = await store.save({
      item_id: "z",
      kind: "question",
      text: "第一段问题",
      quote: "来源😀",
    });
    await store.save({
      item_id: "z",
      kind: "ai",
      text: "模型回答",
      parent_id: parent.id,
      question: parent.text,
      reply: {
        answer: "模型回答",
        model: "test-model",
        quotes: [{ page: 2, block_id: "b", quote: "确切依据" }],
      },
    });
    const all = readingNotesMarkdown(store.getSnapshot().items, report);
    expect(all.indexOf("第一段问题")).toBeLessThan(all.indexOf("模型回答"));
    expect(all.indexOf("模型回答")).toBeLessThan(all.indexOf("后一段笔记"));
    expect(all).toContain("test-model");
    expect(all).toContain("确切依据");
    const mine = readingNotesMarkdown(store.getSnapshot().items, report, false);
    expect(mine).not.toContain("模型回答");
    expect(mine).toContain("第一段问题");
  });

  it("keeps multiline selected text and answer evidence within their Markdown containers", async () => {
    const report = fixture();
    const store = createReadingNotesStore("multiline-export", report, false);
    const saved = await store.save({
      item_id: "z",
      kind: "ai",
      text: "回答",
      quote: "第一行\r\n\r\n## 标题",
      reply: {
        answer: "回答",
        quotes: [{ page: 2, block_id: "b", quote: "依据\r\n\r\n- 原文列表\r结尾" }],
      },
    });
    const md = readingNotesMarkdown([saved], report);
    expect(md).toContain("> 第一行\n> \n> ## 标题");
    expect(md).toContain("- PDF 第 2 页 · 依据\n  \n  - 原文列表\n  结尾");
    expect(md).not.toContain("\r");
  });
});
