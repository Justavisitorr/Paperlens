import { useEffect, useRef, useState, type ReactNode } from "react";
import "./styles/23-reading-notes.css";
import type { DashboardReport, ReadingItem } from "./types";
import { Markdown } from "./markdown";
import { TexText } from "./AnalysisText";
import { AnnotationStylePicker } from "./AnnotationCard";
import type { AnnotationAppearance } from "./api";
import { downloadFile, fileStem } from "./export";
import {
  readingNoteLabel,
  READING_NOTE_MAX,
  readingNotesKey,
  readingNotesInOrder,
  readingNotesMarkdown,
  type ReadingNote,
  type ReadingNoteInput,
  type ReadingNoteKind,
} from "./readingNotes";

export interface NoteComposerProps {
  paperId: string;
  report: DashboardReport;
  item: ReadingItem;
  kind?: "note" | "question";
  quote?: string;
  note?: ReadingNote;
  canAI?: boolean;
  allowAIReview?: boolean;
  onSave: (input: ReadingNoteInput, id?: string, askAI?: boolean) => Promise<unknown>;
  onClose: () => void;
}
export function noteDraftKey(
  paperId: string,
  report: DashboardReport,
  itemId: string,
  noteId?: string,
) {
  return `${readingNotesKey(paperId, report)}:draft:${encodeURIComponent(noteId || itemId)}`;
}
export function readNoteDraft(
  key: string,
): { text: string; kind: "note" | "question" } | null {
  try {
    const draft = JSON.parse(localStorage.getItem(key) || "null");
    return typeof draft?.text === "string" && ["note", "question"].includes(draft.kind)
      ? draft
      : null;
  } catch {
    return null;
  }
}
export function NoteComposer({
  paperId,
  report,
  item,
  kind = "note",
  quote = "",
  note,
  canAI = false,
  onSave,
  onClose,
}: NoteComposerProps) {
  const key = noteDraftKey(paperId, report, item.id, note?.id);
  const [draft] = useState(() => readNoteDraft(key));
  const [text, setText] = useState(draft?.text ?? note?.text ?? "");
  const [mode] = useState<"note" | "question">(
    draft?.kind ?? (note?.kind === "question" ? "question" : kind),
  );
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const maxLength = mode === "question" ? 4000 : READING_NOTE_MAX;
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify({ text, kind: mode }));
    } catch {
      setError("草稿未能保存到浏览器，请保留当前输入或复制备份。");
    }
  }, [key, text, mode]);
  const save = async (askAI = false) => {
    if (!text.trim() || busy) return;
    if (text.length > maxLength) {
      setError(
        `请将${mode === "question" ? "问题" : "笔记"}控制在 ${maxLength} 字以内。`,
      );
      return;
    }
    setBusy(true);
    setError("");
    try {
      await onSave(
        {
          item_id: item.id,
          kind: mode,
          text: text.trim(),
          quote: note?.quote || quote,
          parent_id: note?.parent_id || null,
          question: note?.question || "",
          reply: note?.reply || null,
          ...(note?.mark && mode === "note" ? { mark: note.mark } : {}),
        },
        note?.id,
        askAI,
      );
      try {
        localStorage.removeItem(key);
      } catch {
        /* The saved note remains authoritative. */
      }
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section
      className="note-composer"
      role="dialog"
      aria-label={note ? "编辑笔记" : "写笔记"}
    >
      <header className="reading-note-head">
        <strong>
          {note ? "编辑" : "添加"}
          {mode === "question" ? "问题" : "笔记"}
        </strong>
        <button
          className="icon-button small"
          aria-label="关闭笔记编辑"
          onClick={onClose}
          disabled={busy}
        >
          ×
        </button>
      </header>
      {(note?.quote || quote) && (
        <blockquote>
          <TexText text={note?.quote || quote} />
        </blockquote>
      )}
      <textarea
        autoFocus
        rows={5}
        maxLength={maxLength}
        value={text}
        disabled={busy}
        aria-label={mode === "question" ? "我的问题" : "我的笔记"}
        placeholder={
          mode === "question" ? "写下疑问，之后再请 AI 回答…" : "写下理解、想法或联想…"
        }
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing) return;
          if (e.key === "Escape") {
            e.preventDefault();
            if (!busy) onClose();
          }
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            void save();
          }
        }}
      />
      <p className="note-composer-hint">支持格式与公式，草稿自动保存。</p>
      {error && (
        <p role="alert" className="error-line">
          {error}
        </p>
      )}
      <footer className="reading-note-actions">
        <button className="button small" disabled={busy} onClick={onClose}>
          取消
        </button>
        <button
          className="button small primary"
          disabled={busy || !text.trim() || text.length > maxLength}
          onClick={() => void save()}
        >
          {busy ? "保存中…" : "保存"}
        </button>
        {canAI && (
          <button
            className="button small"
            disabled={busy || !text.trim() || text.length > maxLength}
            onClick={() => void save(true)}
          >
            保存并请 AI 回复
          </button>
        )}
      </footer>
    </section>
  );
}

export type NoteAskKind = "answer" | "review" | "followup";
export type NoteAsk = (
  note: ReadingNote,
  question: string,
  kind?: NoteAskKind,
) => Promise<boolean> | boolean | void;
export const followupDraftKey = (noteId: string) =>
  `paperlens:reading-note-followup:${noteId}`;
export function readFollowupDraft(noteId: string): string {
  try {
    return localStorage.getItem(followupDraftKey(noteId)) || "";
  } catch {
    return "";
  }
}
export async function submitNoteFollowup(
  note: ReadingNote,
  question: string,
  onAsk: NoteAsk,
): Promise<boolean> {
  if (!question.trim() || question.length > 4000)
    throw new Error("请填写不超过 4000 字的追问。");
  // Keep the question even when generation stops, fails, or completes without a durable answer.
  localStorage.setItem(followupDraftKey(note.id), question);
  const saved = await onAsk(note, question.trim(), "followup");
  if (saved !== true) return false;
  try {
    localStorage.removeItem(followupDraftKey(note.id));
  } catch {
    /* The completed reply remains saved. */
  }
  return true;
}
export interface NotePending {
  text?: string;
  error?: string;
  onStop?: () => void;
  onRetrySave?: () => void;
}
export interface NoteCardProps {
  note: ReadingNote;
  replies?: ReadingNote[];
  children?: ReactNode;
  canAI?: boolean;
  onAsk?: NoteAsk;
  onEdit?: (note: ReadingNote) => void;
  onDelete?: (note: ReadingNote) => void;
  onJump?: (note: ReadingNote) => void;
  pending?: NotePending;
  replyCount?: number;
  busy?: boolean;
  onChangeMark?: (
    note: ReadingNote,
    appearance: AnnotationAppearance,
  ) => Promise<unknown>;
  onRemoveMark?: (note: ReadingNote) => Promise<unknown>;
}
export function NoteCard({
  note,
  replies = [],
  children,
  canAI = false,
  onAsk,
  onEdit,
  onDelete,
  onJump,
  pending,
  replyCount = replies.length,
  busy = false,
  onChangeMark,
  onRemoveMark,
}: NoteCardProps) {
  const [draft] = useState(() => readFollowupDraft(note.id));
  const [expanded, setExpanded] = useState(false),
    [followup, setFollowup] = useState(Boolean(draft)),
    [question, setQuestion] = useState(draft),
    [sending, setSending] = useState(false),
    [sendError, setSendError] = useState("");
  const sendingRef = useRef(false);
  const isBusy = busy || sending || Boolean(pending?.onStop);
  useEffect(() => {
    try {
      if (question) localStorage.setItem(followupDraftKey(note.id), question);
      else localStorage.removeItem(followupDraftKey(note.id));
    } catch {
      setSendError("追问草稿无法保存到浏览器，请复制保留。");
    }
  }, [note.id, question]);
  const ai = note.kind === "ai" || note.kind === "check";
  const long = ai && note.text.length > 450;
  const ask = async (kind: NoteAskKind) => {
    const value = kind === "followup" ? question.trim() : note.text || note.quote;
    if (!value || !onAsk || isBusy || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    setSendError("");
    try {
      const saved =
        kind === "followup"
          ? await submitNoteFollowup(note, question, onAsk)
          : await onAsk(note, value, kind);
      if (kind === "followup" && saved === true) {
        setQuestion("");
        setFollowup(false);
      }
    } catch (e) {
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  };
  return (
    <article
      className={`reading-note-card note-${note.kind}${note.detached ? " is-detached" : ""}`}
      data-note-id={note.id}
      data-item-id={note.item_id}
      data-mark-color={note.mark?.color}
      data-mark-style={note.mark?.style}
    >
      <header className="reading-note-head">
        <strong>{readingNoteLabel(note)}</strong>
        {note.kind === "question" && (
          <span className="note-status">
            {replyCount ? "已回复" : pending && !pending.error ? "正在回答" : "待回答"}
          </span>
        )}
        {onJump && (
          <button
            className="text-button"
            onClick={() => onJump(note)}
            disabled={note.detached}
            title="回到原文段落"
          >
            p.{note.page}
          </button>
        )}
      </header>
      {note.detached && (
        <p className="note-source-warning">原文版本或段落已变化，定位需核对。</p>
      )}
      {note.quote && (
        <blockquote>
          <TexText text={note.quote} />
        </blockquote>
      )}
      {note.question && ai && (
        <div className="note-reply-question">
          <span>问题</span>
          <Markdown text={note.question} />
        </div>
      )}
      {note.text && (
        <div className={`reading-note-body${long && !expanded ? " is-collapsed" : ""}`}>
          <Markdown text={note.text} />
        </div>
      )}
      {note.mark && onChangeMark && (
        <details className="note-mark-settings">
          <summary>
            勾画样式 · {note.mark.style === "underline" ? "下划线" : "高亮"}
          </summary>
          <AnnotationStylePicker
            value={note.mark}
            disabled={sending}
            onChange={(appearance) => {
              setSending(true);
              setSendError("");
              void onChangeMark(note, appearance)
                .catch((e) => setSendError(String(e)))
                .finally(() => setSending(false));
            }}
          />
          {onRemoveMark && (
            <button
              className="text-button"
              disabled={sending}
              onClick={() => {
                setSending(true);
                setSendError("");
                void onRemoveMark(note)
                  .catch((e) => setSendError(String(e)))
                  .finally(() => setSending(false));
              }}
            >
              取消勾画
            </button>
          )}
        </details>
      )}
      {long && (
        <button
          className="text-button note-expand"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "收起全文" : "展开全文"}
        </button>
      )}
      {note.reply && (
        <>
          {note.reply.model && <p className="note-model">{note.reply.model}</p>}
          {!!note.reply.quotes?.length && (
            <details className="note-evidence">
              <summary>原文依据 · {note.reply.quotes.length}</summary>
              {note.reply.quotes.map((q, i) => (
                <blockquote key={`${q.block_id}-${i}`}>
                  <span>PDF 第 {q.page} 页</span>
                  {q.quote}
                </blockquote>
              ))}
            </details>
          )}
          {note.reply.grounded === false && (
            <p className="note-source-warning">回答引句未能在原文中匹配，请核对。</p>
          )}
          {note.reply.warnings?.map((warning, index) => (
            <p key={index} className="note-source-warning">
              {warning}
            </p>
          ))}
        </>
      )}
      <div className="reading-note-actions">
        {!ai && onEdit && (
          <button className="text-button" onClick={() => onEdit(note)}>
            {note.mark && !note.text ? "写笔记" : "编辑"}
          </button>
        )}
        <button
          className="text-button"
          disabled={!canAI || !onAsk || note.detached || isBusy}
          title={
            !canAI
              ? "连接本地服务并配置模型后可回复"
              : note.detached
                ? "原文已变化，请先核对定位"
                : "使用当前配置的模型回复这条笔记"
          }
          onClick={() =>
            ai
              ? setFollowup(!followup)
              : void ask(note.kind === "question" || !note.text ? "answer" : "review")
          }
        >
          AI 回复
        </button>
        {!ai && replyCount > 0 && canAI && onAsk && !note.detached && (
          <button
            className="text-button"
            aria-expanded={followup}
            disabled={isBusy}
            onClick={() => setFollowup(!followup)}
          >
            继续追问
          </button>
        )}
        {onDelete && (
          <button className="text-button" onClick={() => onDelete(note)}>
            删除
          </button>
        )}
      </div>
      {followup && (
        <form
          className="note-followup"
          onSubmit={(e) => {
            e.preventDefault();
            void ask("followup");
          }}
        >
          <textarea
            rows={2}
            maxLength={4000}
            value={question}
            disabled={isBusy}
            aria-label="继续追问"
            placeholder="继续问什么？"
            onChange={(e) => setQuestion(e.target.value)}
          />
          <button
            className="button small"
            type="submit"
            disabled={isBusy || !question.trim() || question.length > 4000}
          >
            发送追问
          </button>
        </form>
      )}
      {sendError && (
        <p className="error-line" role="alert">
          {sendError}
        </p>
      )}
      {pending && (
        <div className="note-pending" aria-live="polite">
          {pending.text && <div className="note-stream-preview">{pending.text}</div>}
          {pending.error ? (
            <p className="error-line" role="alert">
              {pending.error}
            </p>
          ) : (
            <p>正在生成 · 引文尚未核对</p>
          )}
          {pending.onStop && (
            <button className="text-button" onClick={pending.onStop}>
              停止生成
            </button>
          )}
          {pending.error && pending.onRetrySave && (
            <button className="text-button" onClick={pending.onRetrySave}>
              重试保存
            </button>
          )}
        </div>
      )}
      {!!replies.length && (
        <div className="note-replies">
          {replies.map((reply) => (
            <NoteCard
              key={reply.id}
              note={reply}
              canAI={canAI}
              onAsk={onAsk}
              onDelete={onDelete}
              onJump={onJump}
              busy={isBusy}
            />
          ))}
        </div>
      )}
      {children}
    </article>
  );
}

export type NotesFilter = "all" | "mine" | "marks" | "automatic" | ReadingNoteKind;
export const NOTES_FILTERS: [NotesFilter, string][] = [
  ["all", "全部笔记"],
  ["marks", "全部勾画"],
  ["note", "我的笔记"],
  ["question", "我的问题"],
  ["automatic", "自动笔记"],
  ["ai", "AI 回答"],
  ["check", "核对提示"],
];
export function filterReadingNotes(
  notes: ReadingNote[],
  filter: NotesFilter,
  query: string,
): ReadingNote[] {
  const text = query.trim().toLocaleLowerCase();
  return notes.filter(
    (n) =>
      (filter === "all" ||
        (filter === "marks"
          ? !!n.mark
          : filter === "mine"
            ? n.kind === "note" || n.kind === "question"
            : filter === "automatic"
              ? !!n.auto_types?.length
              : n.kind === filter && (filter !== "note" || !!n.text))) &&
      (!text ||
        [n.text, n.quote, n.question].some((value) =>
          value.toLocaleLowerCase().includes(text),
        )),
  );
}
export interface NotesPanelProps {
  initialFilter?: NotesFilter;
  focusedNote?: string;
  notes: ReadingNote[];
  report: DashboardReport;
  onClose: () => void;
  onJump?: NoteCardProps["onJump"];
  onEdit?: NoteCardProps["onEdit"];
  onDelete?: NoteCardProps["onDelete"];
  onAsk?: NoteCardProps["onAsk"];
  onChangeMark?: NoteCardProps["onChangeMark"];
  onRemoveMark?: NoteCardProps["onRemoveMark"];
  canAI?: boolean;
  pending?: number;
  pendingById?: Record<string, NotePending>;
  children?: ReactNode;
  error?: string;
  onRetry?: () => void;
}
export function noteRootId(note: ReadingNote, notes: ReadingNote[]): string {
  const byId = new Map(notes.map((n) => [n.id, n]));
  const seen = new Set<string>();
  let current = note;
  while (current.parent_id && !seen.has(current.id)) {
    seen.add(current.id);
    const parent = byId.get(current.parent_id);
    if (!parent) break;
    current = parent;
  }
  return current.id;
}
export function NotesPanel({
  notes,
  report,
  onClose,
  onJump,
  onEdit,
  onDelete,
  onAsk,
  canAI = false,
  pending = 0,
  pendingById = {},
  children,
  error = "",
  onRetry,
  initialFilter = "all",
  focusedNote = "",
  onChangeMark,
  onRemoveMark,
}: NotesPanelProps) {
  const [filter, setFilter] = useState<NotesFilter>(initialFilter),
    [query, setQuery] = useState("");
  const [includeAI, setIncludeAI] = useState(true),
    [excluded, setExcluded] = useState(new Set<string>());
  const ordered = readingNotesInOrder(notes, report);
  const visible = filterReadingNotes(ordered, filter, query);
  const selected = visible.filter((n) => !excluded.has(n.id));
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!focusedNote) return;
    const card = [
      ...(panel.current?.querySelectorAll<HTMLElement>("[data-note-id]") || []),
    ].find((el) => el.dataset.noteId === focusedNote);
    card?.scrollIntoView({ block: "nearest" });
  }, [focusedNote]);
  const exportNotes = () => {
    downloadFile(
      readingNotesMarkdown(selected, report, includeAI),
      `${fileStem(report.analysis.title)}-阅读笔记.md`,
      "text/markdown;charset=utf-8",
    );
  };
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) {
        e.preventDefault();
        onClose();
      }
    };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  }, [onClose]);
  return (
    <aside className="reading-notes-panel" aria-label="阅读笔记" ref={panel}>
      <header className="reading-notes-panel-head">
        <strong>
          阅读笔记 <span>{notes.length}</span>
        </strong>
        <button className="icon-button" aria-label="关闭阅读笔记" onClick={onClose}>
          ×
        </button>
      </header>
      <div className="reading-notes-controls">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索笔记、问题或引用"
          aria-label="搜索阅读笔记"
        />
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value as NotesFilter)}
          aria-label="筛选笔记类型"
        >
          {NOTES_FILTERS.map(([kind, label]) => (
            <option key={kind} value={kind}>
              {label}
            </option>
          ))}
        </select>
        {(pending > 0 || error) && (
          <div className="notes-sync-status" role="status">
            <p>{error || `${pending} 条修改已保存在本浏览器，尚未同步。`}</p>
            {onRetry && (
              <button className="text-button" onClick={onRetry}>
                重试同步
              </button>
            )}
          </div>
        )}
      </div>
      <div className="reading-notes-list">
        {!visible.length && !children && (
          <p className="notes-empty">
            {notes.length ? "没有符合筛选的笔记。" : "选中文字，记下想法或提出问题。"}
          </p>
        )}
        {visible.map((note) => (
          <div className="notes-panel-entry" key={note.id}>
            <label className="note-export-select">
              <input
                type="checkbox"
                checked={!excluded.has(note.id)}
                aria-label={`选择导出${readingNoteLabel(note)}`}
                onChange={(e) =>
                  setExcluded((old) => {
                    const next = new Set(old);
                    e.target.checked ? next.delete(note.id) : next.add(note.id);
                    return next;
                  })
                }
              />
            </label>
            <NoteCard
              note={note}
              canAI={canAI}
              onJump={onJump}
              onEdit={onEdit}
              onDelete={onDelete}
              onAsk={onAsk}
              onChangeMark={onChangeMark}
              onRemoveMark={onRemoveMark}
              pending={note.parent_id ? undefined : pendingById[note.id]}
              busy={Boolean(pendingById[noteRootId(note, notes)]?.onStop)}
              replyCount={notes.filter((n) => n.parent_id === note.id).length}
            />
          </div>
        ))}
        {children}
      </div>
      <footer className="reading-notes-export">
        <label>
          <input
            type="checkbox"
            checked={includeAI}
            onChange={(e) => setIncludeAI(e.target.checked)}
          />
          包含 AI 内容
        </label>
        <button
          className="button small"
          onClick={() =>
            setExcluded(excluded.size ? new Set() : new Set(visible.map((n) => n.id)))
          }
        >
          {excluded.size ? "全选" : "取消全选"}
        </button>
        <button
          className="button small primary"
          disabled={
            !selected.some((n) => includeAI || n.kind === "note" || n.kind === "question")
          }
          onClick={exportNotes}
        >
          导出笔记
        </button>
      </footer>
    </aside>
  );
}
