import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./icons";
import {
  askPapers,
  type ChatHistory,
  type AnnotationAppearance,
  type Annotation,
} from "./api";
import { editAnnotation, removeAnnotation } from "./annotations";
import { askPapersStream } from "./chatStream";
import type { ChatMessage } from "./chat";
import { readingDocument } from "./reading";
import { jumpTo } from "./ReaderViews";
import { go } from "./router";
import {
  NoteCard,
  NoteComposer,
  NotesPanel,
  type NotesFilter,
} from "./ReadingNoteComponents";
import { AutoNotes } from "./AutoNotes";
import {
  useReadingNotes,
  annotationNotes,
  type ReadingNote,
  type ReadingNoteInput,
  type ReadingMark,
} from "./readingNotes";
import type { DashboardReport, ReadingItem } from "./types";
import type { Picked } from "./SelectionExplain";

interface Work {
  text?: string;
  error?: string;
  onStop?: () => void;
  onRetrySave?: () => void;
}

/** A note stays with the object being read; original block selections remain separate. */
export function useNotebook({
  paperId,
  report,
  remote,
  canAI,
  canAutoNotes = false,
  stream,
  onToast,
  annotations = [],
  onOpen,
}: {
  paperId: string;
  report: DashboardReport;
  remote: boolean;
  canAI: boolean;
  canAutoNotes?: boolean;
  stream: boolean;
  onToast: (message: string) => void;
  annotations?: Annotation[];
  onOpen?: () => void;
}) {
  const notes = useReadingNotes(paperId, report, remote);
  const reading = useMemo(() => readingDocument(report), [report]);
  const objects = useMemo(
    () => [
      ...reading.items,
      ...reading.references,
      ...Object.values(reading.information_items || {}).flat(),
    ],
    [reading],
  );
  const [editor, setEditor] = useState<{
    item: ReadingItem;
    kind: "note" | "question";
    quote?: string;
    note?: ReadingNote;
  } | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelFilter, setPanelFilter] = useState<NotesFilter>("all");
  const [focusedNote, setFocusedNote] = useState("");
  const [panelVisit, setPanelVisit] = useState(0);
  const combined = useMemo(
    () => [...notes.items, ...annotationNotes(annotations, report)],
    [notes.items, annotations, report],
  );
  const show = (note?: ReadingNote, filter: NotesFilter = "all") => {
    onOpen?.();
    setPanelVisit((visit) => visit + 1);
    setPanelFilter(filter);
    setFocusedNote(note?.id || "");
    setPanelOpen(true);
  };
  const [autoOpen, setAutoOpen] = useState(false);
  const [work, setWork] = useState<Record<string, Work>>({});
  const running = useRef(new Map<string, AbortController>());
  const checking = useRef(new Set<string>());
  const version = useRef(reading.source_version);
  version.current = reading.source_version;
  const current = useRef<string>("");
  const latest = useRef(notes);
  latest.current = notes;
  useEffect(
    () => () => {
      running.current.forEach((controller) => controller.abort());
      running.current.clear();
    },
    [paperId, reading.source_version],
  );

  const forPick = (picked: Picked) =>
    objects.find((item) =>
      item.parts.some(
        (part) =>
          part.block_id === picked.block_id &&
          part.start <= picked.start &&
          part.end >= picked.end,
      ),
    ) ||
    objects.find((item) => item.parts.some((part) => part.block_id === picked.block_id));
  const jump = (note: ReadingNote) => {
    if (note.detached) {
      onToast("原文已更新，这条笔记仍保留在笔记列表中");
      return;
    }
    const element = document.getElementById(note.item_id);
    if (element) {
      jumpTo(note.item_id);
      return;
    }
    sessionStorage.setItem(
      "paperlens:pending-reading-item",
      JSON.stringify({ paperId, itemId: note.item_id }),
    );
    go({ page: "read", paperId, mode: "text" });
  };
  const open = (item: ReadingItem, kind: "note" | "question", quote?: string) => {
    current.current = item.id;
    setEditor({ item, kind, quote });
  };
  const edit = (note: ReadingNote) => {
    const item = objects.find((item) => item.id === note.item_id);
    if (!item) {
      onToast("对应段落已变化，请导出保留这条笔记");
      return;
    }
    setEditor({
      item,
      kind: note.kind === "question" ? "question" : "note",
      note,
      quote: note.quote,
    });
  };
  const remove = async (note: ReadingNote) => {
    if (!confirm("删除这条笔记及其页边回复？")) return;
    running.current.get(note.id)?.abort();
    try {
      if (note.annotation_id) await removeAnnotation(paperId, note.annotation_id);
      else await notes.remove(note.id);
    } catch (e) {
      onToast(e instanceof Error ? e.message : String(e));
    }
  };

  const ask = async (
    note: ReadingNote,
    question: string,
    kind?: "answer" | "review" | "followup" | "check",
  ) => {
    if (note.parent_id)
      note = latest.current.items.find((n) => n.id === note.parent_id) || note;
    if (!canAI || note.detached || running.current.has(note.id)) return false;
    if (!objects.some((item) => item.id === note.item_id))
      throw new Error("对应原文段落无法定位，请在当前正文重新选择后回复。");
    question = question.slice(0, 4000);
    const requestVersion = reading.source_version;
    const controller = new AbortController();
    running.current.set(note.id, controller);
    const context = note.quote ? `引用的选文：${note.quote.slice(0, 800)}\n` : "";
    const instruction =
      kind === "review"
        ? `请回复我的阅读笔记，区分论文陈述与我的理解，依据原文给出建议。\n${context}我的笔记：`
        : `${context}${kind === "followup" ? `原笔记：${note.text.slice(0, 1200)}\n追问：` : ""}`;
    const prompt = instruction + question.slice(0, 4000 - instruction.length);
    const previous = [
      note,
      ...latest.current.items.filter((n) => n.parent_id === note.id),
    ].filter(
      (n) =>
        n.reply && n.reply.grounded !== false && (n.question || n.auto_types?.length),
    );
    const history: ChatHistory[] = previous.slice(-5).flatMap((n) => [
      {
        role: "user" as const,
        content: (n.question || "请为这处原文写阅读笔记。").slice(0, 4000),
      },
      { role: "assistant" as const, content: n.text.slice(0, 4000) },
    ]);
    const body = {
      papers: [paperId],
      question: prompt.slice(0, 4000),
      history,
      passages: [],
      terms: [],
      reading_items: [
        {
          paper_id: paperId,
          item_id: note.item_id,
          source_version: reading.source_version,
        },
      ],
    };
    const update = (text: string) =>
      setWork((old) => ({
        ...old,
        [note.id]: { text, onStop: () => controller.abort() },
      }));
    update("");
    let completed: ReadingNoteInput | undefined;
    const clear = () =>
      setWork((old) => {
        const next = { ...old };
        delete next[note.id];
        return next;
      });
    const saveCompleted = async () => {
      if (version.current !== requestVersion)
        throw new Error("原文已更新，请核对后再保存这次回答。");
      await latest.current.save(completed!);
      clear();
    };
    try {
      const reply = stream
        ? await askPapersStream(body, controller.signal, update)
        : await askPapers(body, controller.signal);
      if (controller.signal.aborted) throw new DOMException("已停止生成。", "AbortError");
      if (reply.grounded === false)
        throw new Error("这次回答未找到充分原文依据，可以补充问题后再问。");
      completed = {
        item_id: note.item_id,
        kind: kind === "check" ? "check" : "ai",
        parent_id: note.annotation_id ? null : note.id,
        text: reply.answer,
        question,
        quote: note.quote,
        reply,
      };
      await saveCompleted();
      return true;
    } catch (e) {
      setWork((old) => ({
        ...old,
        [note.id]: {
          text: completed?.text,
          onRetrySave: completed
            ? () => void saveCompleted().catch((e) => onToast(String(e)))
            : undefined,
          error: controller.signal.aborted
            ? "已停止，问题与原有回复仍保留。"
            : e instanceof Error
              ? e.message
              : String(e),
        },
      }));
      return false;
    } finally {
      running.current.delete(note.id);
    }
  };
  const save = async (input: ReadingNoteInput, id?: string, askAI?: boolean) => {
    const legacy = combined.find((n) => n.id === id && n.annotation_id);
    if (legacy) {
      const a = annotations.find((a) => a.id === legacy.annotation_id)!;
      const updated = await editAnnotation(paperId, a, { note: input.text });
      setEditor(null);
      if (askAI)
        void ask({ ...legacy, text: updated.note || input.text }, input.text, "review");
      else onToast("笔记已保存");
      return;
    }
    const saved = await notes.save(input, id);
    setEditor(null);
    requestAnimationFrame(() => {
      const details = document
        .getElementById(saved.item_id)
        ?.querySelector<HTMLDetailsElement>(".reading-note-inline");
      if (details) details.open = true;
    });
    if (askAI) void ask(saved, input.text, input.kind === "note" ? "review" : "answer");
    else onToast(remote && !latest.current.error ? "笔记已保存" : "笔记已保存在本浏览器");
  };
  const check = async (item: ReadingItem) => {
    if (checking.current.has(item.id)) return;
    checking.current.add(item.id);
    try {
      const question =
        "请核对这一处原文与上下文的符号定义、公式或数字是否一致。只列有具体依据的疑点，区分原文笔误、识别错误与尚不能确定；不要改写原文。若无明确疑点，请直接说明。";
      const note = await notes.save({
        item_id: item.id,
        kind: "question",
        text: "核对这处原文是否存在不一致",
      });
      await ask(note, question, "check");
    } finally {
      checking.current.delete(item.id);
    }
  };
  const pin = async (answer: ChatMessage, question?: ChatMessage) => {
    const object = question?.refs.find(
      (r) => r.kind === "reading_item" && r.paper_id === paperId,
    );
    const passage = question?.refs.find(
      (r) => r.kind === "passage" && r.paper_id === paperId,
    );
    const quote = answer.reply?.quotes?.find(
      (q) => !q.paper_id || q.paper_id === paperId,
    );
    if (
      object?.kind === "reading_item" &&
      (object.source_version !== reading.source_version ||
        !objects.some((i) => i.id === object.item_id))
    )
      throw new Error("引用的原文版本已变化，请在当前正文重新选择后提问。");
    if (passage?.kind === "passage") {
      const source = report.document.blocks.find((b) => b.id === passage.block_id);
      if (!source || source.text.slice(passage.start, passage.end) !== passage.text)
        throw new Error("引用文字已变化，请先核对当前原文。");
    }
    const target =
      (object?.kind === "reading_item"
        ? objects.find((i) => i.id === object.item_id)
        : undefined) ||
      (passage?.kind === "passage" ? forPick(passage) : undefined) ||
      (quote
        ? objects.find((i) => i.parts.some((p) => p.block_id === quote.block_id))
        : undefined) ||
      objects.find((i) => i.id === current.current);
    if (!target) throw new Error("请先在正文选择一段内容，再把回答放到页边。");
    if (!answer.reply || answer.error || answer.reply.grounded === false)
      throw new Error("请等待回答完成后再保存。");
    await notes.save(
      {
        item_id: target.id,
        kind: "ai",
        text: answer.content,
        question: question?.content || "",
        quote: passage?.kind === "passage" ? passage.text : "",
        reply: answer.reply,
      },
      answer.id,
    );
    onToast("已放到原文页边，可在笔记中继续查看");
  };
  const changeMark = async (note: ReadingNote, appearance: AnnotationAppearance) => {
    if (note.annotation_id) {
      const a = annotations.find((a) => a.id === note.annotation_id)!;
      return editAnnotation(paperId, a, appearance);
    }
    return notes.save({ ...note, mark: { ...note.mark!, ...appearance } }, note.id);
  };
  const removeMark = async (note: ReadingNote) => {
    if (note.annotation_id) {
      // Retain an attached written note before removing its original highlight.
      if (note.text)
        await notes.save(
          { item_id: note.item_id, kind: "note", text: note.text, quote: note.quote },
          note.annotation_id,
        );
      return removeAnnotation(paperId, note.annotation_id);
    }
    return note.text || notes.items.some((n) => n.parent_id === note.id)
      ? notes.save({ ...note, text: note.text || note.quote, mark: null }, note.id)
      : notes.remove(note.id);
  };
  const roots = combined.filter((n) => !n.parent_id);
  const groups = new Map<string, ReadingNote[]>();
  for (const note of roots)
    if (!note.detached)
      groups.set(note.item_id, [...(groups.get(note.item_id) || []), note]);
  const cards = (itemId: string) =>
    (groups.get(itemId) || []).map((note) => (
      <NoteCard
        key={note.id}
        note={note}
        replies={notes.items.filter((n) => n.parent_id === note.id)}
        canAI={canAI}
        onAsk={ask}
        onEdit={edit}
        onDelete={remove}
        onJump={jump}
        onChangeMark={changeMark}
        onRemoveMark={removeMark}
        pending={work[note.id]}
      />
    ));
  return {
    open,
    edit,
    show,
    showAnnotation: (id: string) =>
      show(
        combined.find((n) => n.annotation_id === id),
        "marks",
      ),
    marks: notes.items.filter((n) => n.mark),
    mark: (item: ReadingItem, mark: ReadingMark) =>
      notes.save({
        item_id: item.id,
        kind: "note",
        text: "",
        quote: mark.text.slice(mark.start, mark.end),
        mark,
      }),
    changeMark,
    removeMark,
    forPick,
    pin,
    check,
    canAI,
    panelOpen,
    setCurrent: (id: string) => {
      current.current = id;
    },
    count: (itemId: string) => groups.get(itemId)?.length || 0,
    margin: [...groups.keys()].map((itemId) => ({ itemId, body: cards(itemId) })),
    cards,
    toolbar: (
      <>
        <button className="text-button" onClick={() => show()} aria-label="阅读笔记">
          <Icon name="bookmark" size={14} /> 笔记
          {combined.length ? ` ${combined.length}` : ""}
          {remote && notes.pending > 0 && <span title="尚未同步"> ·</span>}
        </button>
        <button
          className="button"
          onClick={() => show(undefined, "marks")}
          title="查看全部勾画"
        >
          <Icon name="highlighter" size={15} /> 勾画{" "}
          {combined.filter((n) => n.mark).length}
        </button>
        {canAutoNotes && (
          <button className="text-button" onClick={() => setAutoOpen(true)}>
            <Icon name="bolt" size={14} /> 自动笔记
          </button>
        )}
      </>
    ),
    overlays: (
      <>
        {autoOpen && (
          <AutoNotes
            paperId={paperId}
            anchorId={current.current || undefined}
            onSaved={notes.refresh}
            onClose={() => setAutoOpen(false)}
          />
        )}
        {editor && (
          <div
            className="note-editor-backdrop"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setEditor(null);
            }}
          >
            <NoteComposer
              key={editor.note?.id || `${editor.item.id}-${editor.kind}`}
              paperId={paperId}
              report={report}
              {...editor}
              canAI={canAI}
              onSave={save}
              onClose={() => setEditor(null)}
            />
          </div>
        )}
        {panelOpen && (
          <NotesPanel
            key={`${panelFilter}:${panelVisit}`}
            notes={combined}
            initialFilter={panelFilter}
            focusedNote={focusedNote}
            report={report}
            canAI={canAI}
            onClose={() => setPanelOpen(false)}
            onJump={(note) => {
              setPanelOpen(false);
              jump(note);
            }}
            onEdit={edit}
            onDelete={remove}
            onAsk={ask}
            onChangeMark={changeMark}
            onRemoveMark={removeMark}
            pendingById={work}
            pending={remote ? notes.pending : 0}
            error={notes.error}
            onRetry={remote ? notes.retrySync : undefined}
          />
        )}
      </>
    ),
  };
}
