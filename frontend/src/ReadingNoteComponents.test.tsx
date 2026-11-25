import { beforeEach, describe, expect, it, vi } from "vitest";
vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
import { renderToStaticMarkup } from "react-dom/server";
import {
  filterReadingNotes,
  NoteCard,
  NoteComposer,
  NotesPanel,
  noteDraftKey,
  readNoteDraft,
  followupDraftKey,
  readFollowupDraft,
  submitNoteFollowup,
  noteRootId,
  NOTES_FILTERS,
} from "./ReadingNoteComponents";
import type { ReadingNote } from "./readingNotes";
import type { DashboardReport, ReadingItem } from "./types";

it("offers unique note filters and separates bare marks from written notes", () => {
  expect(new Set(NOTES_FILTERS.map(([, label]) => label)).size).toBe(
    NOTES_FILTERS.length,
  );
  const mark = {
    ...note("note"),
    text: "",
    mark: {
      surface: "original" as const,
      text: "word",
      start: 0,
      end: 4,
      color: "blue" as const,
      style: "underline" as const,
    },
  };
  const written = { ...mark, id: "b".repeat(32), text: "我的理解" };
  expect(filterReadingNotes([mark, written], "marks", "")).toHaveLength(2);
  expect(filterReadingNotes([mark, written], "note", "")).toEqual([written]);
  const html = renderToStaticMarkup(
    <NoteCard note={mark} onChangeMark={async () => {}} onRemoveMark={async () => {}} />,
  );
  expect(html).toContain('data-mark-color="blue"');
  expect(html).toContain("勾画样式");
  expect(html).toContain("取消勾画");
});

const note = (kind: ReadingNote["kind"] = "question"): ReadingNote => ({
  id: "a".repeat(32),
  item_id: "reading-1",
  kind,
  text: "为什么 **成立**？",
  quote: "原文引用",
  parent_id: null,
  question: "",
  reply: null,
  created_at: "now",
  updated_at: "now",
  source_version: "v1",
  anchor_text: "原文",
  page: 2,
  detached: false,
});
const report = {
  document: { sha256: "sha", source_name: "p.pdf", blocks: [] },
  analysis: { title: "论文" },
  reading: {
    items: [],
    references: [],
    information: {},
    source_version: "v1",
    hidden_ids: [],
    warnings: [],
  },
} as unknown as DashboardReport;
const item = { id: "reading-1" } as ReadingItem;
beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) || null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  });
});
describe("reading note components", () => {
  it("offers personal-note AI review only when enabled, without disabling questions", () => {
    const props = {
      paperId: "paper",
      report,
      item,
      canAI: true,
      onSave: async () => {},
      onClose: () => {},
    };
    expect(renderToStaticMarkup(<NoteComposer {...props} />)).toContain(
      "保存并请 AI 回复",
    );
    expect(renderToStaticMarkup(<NoteComposer {...props} allowAIReview />)).toContain(
      "保存并请 AI 回复",
    );
    expect(renderToStaticMarkup(<NoteComposer {...props} kind="question" />)).toContain(
      "保存并请 AI 回复",
    );
  });
  it("distinguishes generated reading notes from chat answers without hiding either", () => {
    const automatic: ReadingNote = { ...note("ai"), auto_types: ["summary", "explain"] };
    const answer = { ...note("ai"), id: "b".repeat(32) };
    expect(filterReadingNotes([automatic, answer], "automatic", "")).toEqual([automatic]);
    expect(filterReadingNotes([automatic, answer], "ai", "")).toHaveLength(2);
    expect(renderToStaticMarkup(<NoteCard note={automatic} />)).toContain("AI 阅读笔记");
  });
  it("shows personal questions, answered state and explicit AI actions", () => {
    const html = renderToStaticMarkup(
      <NoteCard note={note()} replyCount={1} canAI onAsk={() => {}} onEdit={() => {}} />,
    );
    expect(html).toContain("我的问题");
    expect(html).toContain("已回复");
    expect(html).toContain("AI 回复");
    expect(html).toContain("继续追问");
    expect(html).toContain("<strong>成立</strong>");
    expect(html).not.toContain("我的筆記");
  });
  it("keeps the saved answer visible during generation failure, collapses long AI text and escapes HTML", () => {
    const ai = {
      ...note("ai"),
      text: "已有答案 <script>evil()</script>\n" + "长回答".repeat(180),
      reply: { answer: "答案", model: "test-model" },
    };
    const html = renderToStaticMarkup(
      <NoteCard note={ai} pending={{ error: "本次生成失败" }} />,
    );
    expect(html).toContain("已有答案");
    expect(html).toContain("本次生成失败");
    expect(html).toContain("is-collapsed");
    expect(html).toContain("展开全文");
    expect(html).toContain("test-model");
    expect(html).not.toContain("<script>");
  });
  it("distinguishes an unsaved preview from completed answers and disables detached AI calls", () => {
    const html = renderToStaticMarkup(
      <NoteCard
        note={{ ...note(), detached: true }}
        canAI
        onAsk={() => {}}
        pending={{ text: "临时内容", onStop: () => {} }}
      />,
    );
    expect(html).toContain("定位需核对");
    expect(html).toContain("引文尚未核对");
    expect(html).toContain("停止生成");
    expect(html).not.toContain("回答问题");
  });
  it("restores composer drafts with source-specific keys and has a separate ordinary save action", () => {
    const key = noteDraftKey("paper", report, item.id);
    localStorage.setItem(key, JSON.stringify({ kind: "question", text: "恢复的问题" }));
    expect(readNoteDraft(key)).toEqual({ kind: "question", text: "恢复的问题" });
    const html = renderToStaticMarkup(
      <NoteComposer
        paperId="paper"
        report={report}
        item={item}
        canAI
        onSave={async () => {}}
        onClose={() => {}}
      />,
    );
    expect(html).toContain("恢复的问题");
    expect(html).toContain("保存并请 AI 回复");
    expect(html).toContain(">保存</button>");
  });
  it("filters personal notes and supports legacy notes when the new list is empty", () => {
    const notes = [note(), { ...note("ai"), id: "b".repeat(32), text: "另一答案" }];
    expect(filterReadingNotes(notes, "mine", "原文引用")).toEqual([notes[0]]);
    expect(filterReadingNotes(notes, "ai", "另一")).toEqual([notes[1]]);
    const html = renderToStaticMarkup(
      <NotesPanel notes={[]} report={report} onClose={() => {}}>
        <p>旧勾画笔记</p>
      </NotesPanel>,
    );
    expect(html).toContain("旧勾画笔记");
    expect(html).not.toContain("写下笔记与问题");
  });

  it("keeps a followup draft during the request, after failure/stop and when completion was not saved", async () => {
    const current = note("ai");
    let finish!: (saved: boolean) => void;
    const result = submitNoteFollowup(
      current,
      "追问的问题",
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    expect(readFollowupDraft(current.id)).toBe("追问的问题");
    finish(false);
    await expect(result).resolves.toBe(false);
    expect(readFollowupDraft(current.id)).toBe("追问的问题");
    await expect(
      submitNoteFollowup(current, "再次追问", () => {
        throw new Error("生成失败");
      }),
    ).rejects.toThrow("生成失败");
    expect(readFollowupDraft(current.id)).toBe("再次追问");
    await expect(submitNoteFollowup(current, "尚未确认保存", () => {})).resolves.toBe(
      false,
    );
    expect(readFollowupDraft(current.id)).toBe("尚未确认保存");
    const restored = renderToStaticMarkup(
      <NoteCard note={current} canAI onAsk={() => false} />,
    );
    expect(restored).toContain("尚未确认保存");
    expect(restored).toContain('class="note-followup"');
  });

  it("clears the followup only after the final answer was saved and does not ask if local storage fails", async () => {
    const current = note("ai"),
      ask = vi.fn(async () => true);
    await expect(submitNoteFollowup(current, "已保存回答的问题", ask)).resolves.toBe(
      true,
    );
    expect(localStorage.getItem(followupDraftKey(current.id))).toBeNull();
    expect(ask).toHaveBeenCalledWith(current, "已保存回答的问题", "followup");
    vi.stubGlobal("localStorage", {
      setItem: () => {
        throw new Error("quota");
      },
    });
    await expect(submitNoteFollowup(current, "不能保存草稿", ask)).rejects.toThrow(
      "quota",
    );
    expect(ask).toHaveBeenCalledTimes(1);
  });

  it("blocks child followup actions while its root is generating and shows the stream once", () => {
    const parent = note(),
      child = { ...note("ai"), id: "b".repeat(32), parent_id: parent.id };
    localStorage.setItem(followupDraftKey(child.id), "保留的追问");
    expect(noteRootId(child, [parent, child])).toBe(parent.id);
    const html = renderToStaticMarkup(
      <NotesPanel
        notes={[parent, child]}
        report={report}
        canAI
        onClose={() => {}}
        onAsk={() => false}
        pendingById={{ [parent.id]: { text: "唯一临时输出", onStop: () => {} } }}
      />,
    );
    expect(html.split("唯一临时输出")).toHaveLength(2);
    expect(html).toMatch(/disabled=""[^>]*>[\s]*AI 回复/);
    expect(html).toMatch(/disabled=""[^>]*>发送追问/);
    expect(html).toContain("保留的追问");
  });

  it("offers retry-save without generating and distinguishes question length from note length", () => {
    const html = renderToStaticMarkup(
      <NoteCard
        note={note()}
        pending={{ text: "已经完成的回答", error: "保存失败", onRetrySave: () => {} }}
      />,
    );
    expect(html).toContain("已经完成的回答");
    expect(html).toContain("重试保存");
    const question = renderToStaticMarkup(
      <NoteComposer
        paperId="p"
        report={report}
        item={item}
        kind="question"
        onSave={async () => {}}
        onClose={() => {}}
      />,
    );
    const personal = renderToStaticMarkup(
      <NoteComposer
        paperId="p"
        report={report}
        item={item}
        kind="note"
        onSave={async () => {}}
        onClose={() => {}}
      />,
    );
    expect(question).toContain('maxLength="4000"');
    expect(personal).toContain('maxLength="12000"');
  });
});
