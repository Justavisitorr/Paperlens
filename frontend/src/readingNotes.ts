import { useEffect, useSyncExternalStore } from "react";
import type { Annotation, AutoNoteType, ChatReply } from "./api";
import type { DashboardReport, ReadingItem } from "./types";
import { readingDocument, readingText } from "./reading";
import { markdownListItem, markdownQuote } from "./markdownExport";

export type ReadingNoteKind = "note" | "question" | "ai" | "check";
export const READING_NOTE_MAX = 12000;
export interface ReadingMark {
  surface: "original" | "translation";
  /** Visible paragraph text and UTF-16 offsets, independent of PDF extraction. */
  text: string;
  start: number;
  end: number;
  color: "yellow" | "green" | "blue" | "pink";
  style: "highlight" | "underline";
}
export interface ReadingNote {
  /** View-only bridge to the original highlight store; never saved as a reading note. */
  annotation_id?: string;
  id: string;
  item_id: string;
  kind: ReadingNoteKind;
  text: string;
  quote: string;
  parent_id: string | null;
  question: string;
  reply: ChatReply | null;
  created_at: string;
  updated_at: string;
  source_version: string;
  anchor_text: string;
  page: number;
  detached: boolean;
  auto_types?: AutoNoteType[];
  mark?: ReadingMark | null;
}
export function annotationNotes(
  annotations: Annotation[],
  report: DashboardReport,
): ReadingNote[] {
  const items = readingNoteItems(report);
  const blocks = new Map(report.document.blocks.map((b) => [b.id, b]));
  return annotations.map((a) => {
    const s = a.selection;
    const block = blocks.get(s.block_id);
    const item =
      items.find((i) =>
        i.parts.some(
          (p) => p.block_id === s.block_id && p.start <= s.start && p.end >= s.end,
        ),
      ) || items.find((i) => i.parts.some((p) => p.block_id === s.block_id));
    return {
      id: `annotation:${a.id}`,
      annotation_id: a.id,
      item_id: item?.id || `block-${s.block_id}`,
      kind: "note",
      text: a.note || "",
      quote: s.text,
      parent_id: null,
      question: "",
      reply: null,
      page: a.page,
      created_at: a.created_at,
      updated_at: a.updated_at || a.created_at,
      source_version: readingDocument(report).source_version,
      anchor_text: item ? readingNoteAnchor(item, report) : "",
      detached:
        !block ||
        block.text.slice(s.start, s.end) !== s.text ||
        !!(a.document_sha256 && a.document_sha256 !== report.document.sha256),
      // This appearance is for cards/export only; the original renderer paints it.
      mark: {
        surface: "original",
        text: s.text,
        start: 0,
        end: s.text.length,
        color: a.color || "yellow",
        style: a.style || "underline",
      },
    };
  });
}
export interface ReadingNoteInput {
  item_id: string;
  kind: ReadingNoteKind;
  text: string;
  quote?: string;
  parent_id?: string | null;
  question?: string;
  reply?: ChatReply | null;
  auto_types?: AutoNoteType[];
  mark?: ReadingMark | null;
}
export const readingNotesKey = (paperId: string, report: DashboardReport) =>
  `paperlens:reading-notes:${encodeURIComponent(paperId)}:${report.document.sha256}`;
export function readingNoteItems(report: DashboardReport): ReadingItem[] {
  const doc = readingDocument(report);
  return [
    ...doc.items,
    ...doc.references,
    ...Object.values(doc.information_items || {}).flat(),
  ];
}
export function readingNoteAnchor(item: ReadingItem, report: DashboardReport): string {
  const text = readingText(item, new Map(report.document.blocks.map((b) => [b.id, b])));
  if (!item.table) return text;
  // Same semantic fields and order as notes.anchor_text; geometry is not a text anchor.
  const table = {
    rows: item.table.rows,
    cols: item.table.cols,
    cells: item.table.cells.map((cell) => ({
      text: cell.text,
      row_start: cell.row_start,
      row_end: cell.row_end,
      col_start: cell.col_start,
      col_end: cell.col_end,
      column_header: !!cell.column_header,
    })),
  };
  return `${text}\n${JSON.stringify(table)}`;
}
export function checkReadingNote(
  note: ReadingNote,
  report: DashboardReport,
): ReadingNote {
  const item = readingNoteItems(report).find((i) => i.id === note.item_id);
  const detached =
    !item ||
    note.source_version !== readingDocument(report).source_version ||
    note.anchor_text !== readingNoteAnchor(item, report);
  return { ...note, detached: note.detached || detached };
}
export function readingNotesInOrder(
  notes: ReadingNote[],
  report: DashboardReport,
): ReadingNote[] {
  const order = new Map(readingNoteItems(report).map((item, index) => [item.id, index]));
  const sorted = [...notes].sort(
    (a, b) =>
      (order.get(a.item_id) ?? Infinity) - (order.get(b.item_id) ?? Infinity) ||
      a.created_at.localeCompare(b.created_at) ||
      a.id.localeCompare(b.id),
  );
  // Replies follow their parent even when created much later than another note.
  const roots = sorted.filter(
    (n) => !n.parent_id || !notes.some((p) => p.id === n.parent_id),
  );
  const out: ReadingNote[] = [],
    visited = new Set<string>();
  const visit = (n: ReadingNote) => {
    if (visited.has(n.id)) return;
    visited.add(n.id);
    out.push(n);
    sorted.filter((child) => child.parent_id === n.id).forEach(visit);
  };
  roots.forEach(visit);
  sorted.forEach(visit);
  return out;
}
export const READING_NOTE_LABELS: Record<ReadingNoteKind, string> = {
  note: "我的笔记",
  question: "我的问题",
  ai: "AI 回答",
  check: "核对原文提示",
};
export const readingNoteLabel = (note: ReadingNote) =>
  note.mark && !note.text
    ? "我的勾画"
    : note.auto_types?.length
      ? "AI 阅读笔记"
      : READING_NOTE_LABELS[note.kind];
export function readingNotesMarkdown(
  notes: ReadingNote[],
  report: DashboardReport,
  includeAI = true,
): string {
  const selected = readingNotesInOrder(notes, report).filter(
    (n) => includeAI || ["note", "question"].includes(n.kind),
  );
  const lines = [
    `# ${report.analysis.title} · 阅读笔记`,
    "",
    `来源：${report.document.source_name}`,
    `文档 SHA-256：${report.document.sha256}`,
    "",
  ];
  const doc = readingDocument(report);
  const sections = new Map<string, string>();
  let section = "正文";
  for (const item of readingNoteItems(report)) {
    if (item.kind === "heading")
      section =
        readingText(item, new Map(report.document.blocks.map((b) => [b.id, b]))) ||
        item.label;
    if (doc.references.some((r) => r.id === item.id)) section = "参考文献";
    sections.set(item.id, section);
  }
  let previous = "";
  for (const note of selected) {
    const next = sections.get(note.item_id) || "原定位已变化";
    if (previous !== next) {
      lines.push(`## ${next}`, "");
      previous = next;
    }
    lines.push(`### ${readingNoteLabel(note)} · PDF 第 ${note.page} 页`, "");
    if (note.quote) lines.push(markdownQuote(note.quote), "");
    if (note.mark)
      lines.push(
        `勾画：${note.mark.color} · ${note.mark.style === "underline" ? "下划线" : "高亮"}`,
        "",
      );
    if (note.question) lines.push(`问题：${note.question}`, "");
    lines.push(
      note.text,
      "",
      `定位：${note.item_id}${note.detached ? "（来源已变化，需核对）" : ""}`,
      `来源版本：${note.source_version}`,
      "",
    );
    if (note.reply?.model) lines.push(`模型：${note.reply.model}`, "");
    if (note.reply?.quotes?.length) {
      lines.push("原文依据：", "");
      note.reply.quotes.forEach((q) =>
        lines.push(markdownListItem(`PDF 第 ${q.page} 页 · ${q.quote}`)),
      );
      lines.push("");
    }
    if (note.reply?.warnings?.length)
      lines.push(...note.reply.warnings.map((w) => `提示：${w}`), "");
  }
  return lines.join("\n");
}

type Op = { token: string; id: string; input: ReadingNoteInput | null };
export interface ReadingNotesSnapshot {
  items: ReadingNote[];
  pending: number;
  error: string;
}
const newId = () => crypto.randomUUID().replace(/-/g, "");
const asInput = (note: ReadingNote): ReadingNoteInput => ({
  item_id: note.item_id,
  kind: note.kind,
  text: note.text,
  quote: note.quote,
  parent_id: note.parent_id,
  question: note.question,
  reply: note.reply,
  ...(note.mark ? { mark: note.mark } : {}),
  ...(note.auto_types?.length ? { auto_types: note.auto_types } : {}),
});
const validNote = (n: ReadingNote): boolean =>
  !!n &&
  /^[a-f0-9]{32}$/.test(n.id) &&
  typeof n.item_id === "string" &&
  ["note", "question", "ai", "check"].includes(n.kind) &&
  [
    n.text,
    n.quote,
    n.question,
    n.created_at,
    n.updated_at,
    n.source_version,
    n.anchor_text,
  ].every((v) => typeof v === "string") &&
  (n.parent_id === null || typeof n.parent_id === "string") &&
  Number.isInteger(n.page);
/** Local writes are durable before a network request. Only acknowledged operations leave the outbox. */
export function createReadingNotesStore(
  paperId: string,
  initialReport: DashboardReport,
  initialRemote: boolean,
) {
  let report = initialReport,
    remote = initialRemote;
  const key = readingNotesKey(paperId, report);
  let items: ReadingNote[] = [],
    outbox: Op[] = [],
    error = "";
  let syncing: Promise<void> | null = null,
    refreshing: Promise<void> | null = null;
  const listeners = new Set<() => void>();
  try {
    const saved = JSON.parse(localStorage.getItem(key) || "null");
    if (Array.isArray(saved?.items) && Array.isArray(saved?.outbox)) {
      items = saved.items.filter(validNote);
      outbox = saved.outbox.filter(
        (op: Op) =>
          op &&
          typeof op.token === "string" &&
          /^[a-f0-9]{32}$/.test(op.id) &&
          (op.input === null ||
            (typeof op.input?.text === "string" &&
              typeof op.input?.item_id === "string")),
      );
    }
  } catch {
    error = "浏览器笔记存储无法读取；请先导出已有笔记。";
  }
  const snapshot = (): ReadingNotesSnapshot => ({
    items: readingNotesInOrder(
      items.map((n) => checkReadingNote(n, report)),
      report,
    ),
    pending: remote ? outbox.length : 0,
    error,
  });
  let state = snapshot();
  const emit = () => {
    state = snapshot();
    listeners.forEach((fn) => fn());
  };
  const persist = (nextItems = items, nextOutbox = outbox) => {
    try {
      localStorage.setItem(key, JSON.stringify({ items: nextItems, outbox: nextOutbox }));
    } catch {
      throw new Error("浏览器存储失败，笔记尚未保存。请导出或腾出存储空间。 ");
    }
  };
  const endpoint = `/api/library/${encodeURIComponent(paperId)}/notes`;
  const request = async (path: string, method = "GET", input?: ReadingNoteInput) => {
    const result = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      ...(input ? { body: JSON.stringify(input) } : {}),
    });
    if (!result.ok) {
      const data = await result.json().catch(() => ({}));
      throw new Error(data.detail || `笔记同步失败（${result.status}）`);
    }
    return result.json();
  };
  const retrySync = (): Promise<void> => {
    if (!remote || !outbox.length) return Promise.resolve();
    if (syncing) return syncing;
    syncing = Promise.resolve().then(async () => {
      try {
        while (outbox.length) {
          // Skip changed anchors without blocking synchronization of other notes.
          const op = outbox.find((candidate) => {
            const local = items.find((n) => n.id === candidate.id);
            return (
              !candidate.input || !local || !checkReadingNote(local, report).detached
            );
          });
          // A queued note must not silently acquire a different edition's server anchor.
          if (!op)
            throw new Error("原文定位已变化，这条笔记保留在本浏览器，请先核对来源。");
          const value = await request(
            `${endpoint}/${op.id}`,
            op.input ? "PUT" : "DELETE",
            op.input || undefined,
          );
          const nextOutbox = outbox.filter((pending) => pending.token !== op.token);
          const returned = value.note || value;
          const nextItems =
            op.input && !nextOutbox.some((o) => o.id === op.id) && returned.id
              ? items.map((n) => (n.id === op.id ? returned : n))
              : items;
          persist(nextItems, nextOutbox);
          items = nextItems;
          outbox = nextOutbox;
          error = "";
          emit();
        }
      } catch (e) {
        error = `已保存在本浏览器，尚未同步：${e instanceof Error ? e.message : e}`;
        emit();
      } finally {
        syncing = null;
      }
    });
    return syncing;
  };
  const refresh = (): Promise<void> => {
    if (!remote) {
      emit();
      return Promise.resolve();
    }
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        // Complete older acknowledged writes before loading the server list.
        if (syncing) await syncing;
        const value = await request(endpoint);
        const byId = new Map<string, ReadingNote>(
          (value.notes || []).map((n: ReadingNote) => [n.id, n]),
        );
        // Pending local edits win; pending deletes also hide server children.
        for (const op of outbox) {
          if (op.input) {
            const n = items.find((item) => item.id === op.id);
            if (n) byId.set(n.id, n);
          } else {
            byId.delete(op.id);
            for (const [id, n] of byId) if (n.parent_id === op.id) byId.delete(id);
          }
        }
        const nextItems = [...byId.values()];
        persist(nextItems);
        items = nextItems;
        error = outbox.length ? "已保存在本浏览器，尚未同步。" : "";
        emit();
        await retrySync();
      } catch (e) {
        error = `无法同步笔记：${e instanceof Error ? e.message : e}`;
        emit();
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  };
  const save = async (input: ReadingNoteInput, id?: string): Promise<ReadingNote> => {
    const maxLength = input.kind === "question" ? 4000 : READING_NOTE_MAX;
    if ((!input.text.trim() && !input.mark) || input.text.length > maxLength)
      throw new Error(
        `请填写不超过 ${maxLength} 字的${input.kind === "question" ? "问题" : "笔记"}。`,
      );
    const item = readingNoteItems(report).find((i) => i.id === input.item_id);
    if (!item) throw new Error("原段落不存在，请刷新后核对。");
    if (id && !/^[a-f0-9]{32}$/.test(id)) throw new Error("笔记编号无效。");
    const old = id ? items.find((n) => n.id === id) : undefined;
    if (old && old.item_id !== input.item_id)
      throw new Error("不能改变已有笔记的原文定位。");
    if (old && (input.parent_id || null) !== old.parent_id)
      throw new Error("不能改变已有回复所属的笔记。");
    if (
      input.parent_id &&
      !items.some((n) => n.id === input.parent_id && n.item_id === input.item_id)
    )
      throw new Error("回复对应的笔记不存在或属于另一段落。");
    const now = new Date().toISOString();
    const note: ReadingNote = {
      id: id || newId(),
      item_id: item.id,
      kind: input.kind,
      text: input.text.trim(),
      quote: input.quote || "",
      parent_id: input.parent_id || null,
      question: input.question || "",
      reply: input.reply || null,
      auto_types: input.auto_types || old?.auto_types,
      mark: input.mark,
      created_at: old?.created_at || now,
      updated_at: now,
      source_version: old?.source_version || readingDocument(report).source_version,
      anchor_text: old?.anchor_text ?? readingNoteAnchor(item, report),
      page: old?.page || item.page,
      detached: old?.detached || false,
    };
    const nextItems = [...items.filter((n) => n.id !== note.id), note];
    const nextOutbox = [
      ...outbox.filter((o) => o.id !== note.id),
      { token: newId(), id: note.id, input: asInput(note) },
    ];
    persist(nextItems, nextOutbox);
    items = nextItems;
    outbox = nextOutbox;
    error = "";
    emit();
    await retrySync();
    return state.items.find((n) => n.id === note.id) || note;
  };
  const remove = async (id: string) => {
    if (!items.some((n) => n.id === id)) return;
    const ids = new Set([id]);
    let before = 0;
    while (before !== ids.size) {
      before = ids.size;
      items.forEach((n) => {
        if (n.parent_id && ids.has(n.parent_id)) ids.add(n.id);
      });
    }
    const nextItems = items.filter((n) => !ids.has(n.id));
    // Include children as deletes, covering locally created replies already in flight.
    const nextOutbox = [
      ...outbox.filter((o) => !ids.has(o.id)),
      ...[...ids]
        .reverse()
        .map((target) => ({ token: newId(), id: target, input: null })),
    ];
    persist(nextItems, nextOutbox);
    items = nextItems;
    outbox = nextOutbox;
    error = "";
    emit();
    await retrySync();
  };
  return {
    save,
    remove,
    refresh,
    retrySync,
    subscribe: (fn: () => void) => {
      listeners.add(fn);
      return () => {
        listeners.delete(fn);
      };
    },
    getSnapshot: () => state,
    updateContext(nextReport: DashboardReport, nextRemote: boolean) {
      if (report !== nextReport || remote !== nextRemote) {
        report = nextReport;
        remote = nextRemote;
        emit();
      }
    },
  };
}
const stores = new Map<string, ReturnType<typeof createReadingNotesStore>>();
export function useReadingNotes(
  paperId: string,
  report: DashboardReport,
  remote: boolean,
) {
  const key = readingNotesKey(paperId, report);
  let store = stores.get(key);
  if (!store) {
    store = createReadingNotesStore(paperId, report, remote);
    stores.set(key, store);
  }
  const current = store;
  const state = useSyncExternalStore(current.subscribe, current.getSnapshot);
  useEffect(() => {
    current.updateContext(report, remote);
    void current.refresh();
  }, [current, report, remote]);
  return {
    ...state,
    save: current.save,
    remove: current.remove,
    refresh: current.refresh,
    retrySync: current.retrySync,
  };
}
