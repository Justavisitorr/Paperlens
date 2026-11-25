import { askPapersStream } from "./chatStream";
import { confirmInputBudget } from "./inputBudget";
import { useSyncExternalStore } from "react";
import { markdownListItem, markdownQuote } from "./markdownExport";
import {
  rememberDeletedConversation,
  forgetDeletedConversation,
} from "./conversationUndo";
import {
  askPaper,
  ApiError,
  askPapers,
  deleteConversation,
  getConversation,
  listConversations,
  putConversation,
  type ChatHistory,
  type ChatReply,
  type ConversationSummary,
} from "./api";

/**
 * Conversations about papers: the quick chat beside the reader and the 对话
 * page share this module. Documents follow the schema in
 * src/paperlens/conversation.py so the same
 * object is sent to `PUT /api/conversations/{id}` or kept in localStorage
 * when the service has no conversation storage.
 *
 * Everything in memory lives here rather than in components, so an answer
 * still lands when the reader closes the drawer or leaves the page mid-wait.
 */

export type ChatRef =
  | { kind: "paper"; paper_id: string; title: string }
  | {
      kind: "term";
      term_id: string;
      term: string;
      explanation: string;
      note: string;
      /** The paper it came from, when that paper is in the library. */
      paper_id?: string;
      paper_title: string;
    }
  | {
      kind: "passage";
      paper_id: string;
      paper_title: string;
      block_id: string;
      /** UTF-16 offsets into `document.blocks[].text`. */
      start: number;
      end: number;
      text: string;
      page?: number;
      annotation_id?: string;
    }
  | {
      kind: "reading_item";
      paper_id: string;
      paper_title: string;
      item_id: string;
      source_version: string;
      label: string;
      /** Display only; the server resolves the full object from its identity. */
      text: string;
      page?: number;
    };

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  /** User turns: what was referenced when it was asked. */
  refs: ChatRef[];
  reply: ChatReply | null;
  error: string | null;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  pinned: boolean;
  /** The context every question is asked with; the reader edits it. */
  refs: ChatRef[];
  messages: ChatMessage[];
}

/** Request limits of `POST /api/chat`. */
export const PASSAGE_DRAG_TYPE = "application/x-paperlens-passage";

/** Only our source handles carry offsets; plain dragged text is never treated as evidence. */
export function droppedPassage(
  raw: string,
): Extract<ChatRef, { kind: "passage" }> | null {
  try {
    if (raw.length > 24000) return null;
    const value = JSON.parse(raw);
    if (
      value.kind !== "passage" ||
      typeof value.paper_id !== "string" ||
      !value.paper_id ||
      typeof value.paper_title !== "string" ||
      typeof value.block_id !== "string" ||
      !value.block_id ||
      typeof value.text !== "string" ||
      !value.text ||
      value.text.length > 4000 ||
      !Number.isInteger(value.start) ||
      !Number.isInteger(value.end) ||
      value.start < 0 ||
      value.end <= value.start ||
      value.end - value.start !== value.text.length
    )
      return null;
    return {
      kind: "passage",
      paper_id: value.paper_id,
      paper_title: value.paper_title,
      block_id: value.block_id,
      start: value.start,
      end: value.end,
      text: value.text,
      ...(Number.isInteger(value.page) && value.page > 0 ? { page: value.page } : {}),
    };
  } catch {
    return null;
  }
}

export const LIMITS = {
  papers: 6,
  passages: 6,
  reading_items: 6,
  terms: 20,
  history: 12,
  turn: 4000,
};

/* ---------- pure helpers (tested) ---------- */

export const newId = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");

export const refKey = (ref: ChatRef) =>
  ref.kind === "paper"
    ? `paper:${ref.paper_id}`
    : ref.kind === "term"
      ? `term:${ref.term_id}`
      : ref.kind === "reading_item"
        ? `reading_item:${ref.paper_id}:${ref.item_id}:${ref.source_version}`
        : `passage:${ref.paper_id}:${ref.block_id}:${ref.start}:${ref.end}`;

export function blankConversation(refs: ChatRef[] = []): Conversation {
  const now = new Date().toISOString();
  return {
    id: newId(),
    title: "",
    created_at: now,
    updated_at: now,
    pinned: false,
    refs,
    messages: [],
  };
}

export const titleFrom = (question: string) => {
  const line = question.replace(/\s+/g, " ").trim();
  return line.length > 28 ? `${line.slice(0, 28)}…` : line;
};

/**
 * Adds a reference, keeping the request inside its limits. A source brings
 * its paper along; passage and reading-object limits rotate independently.
 */
export function addRef(
  refs: ChatRef[],
  ref: ChatRef,
): { refs: ChatRef[]; error?: string } {
  if (refs.some((r) => refKey(r) === refKey(ref))) return { refs };
  // Reselecting an object after reparsing replaces its stale version.
  let next = refs.filter(
    (r) =>
      !(
        ref.kind === "reading_item" &&
        r.kind === "reading_item" &&
        r.paper_id === ref.paper_id &&
        r.item_id === ref.item_id
      ),
  );
  if (
    (ref.kind === "passage" || ref.kind === "reading_item") &&
    !next.some((r) => r.kind === "paper" && r.paper_id === ref.paper_id)
  ) {
    const paper = addRef(next, {
      kind: "paper",
      paper_id: ref.paper_id,
      title: ref.paper_title,
    });
    if (paper.error) return { refs, error: paper.error };
    next = paper.refs;
  }
  const count = (kind: ChatRef["kind"]) => next.filter((r) => r.kind === kind).length;
  if (ref.kind === "paper" && count("paper") >= LIMITS.papers)
    return { refs, error: `一次最多引用 ${LIMITS.papers} 篇论文。` };
  if (ref.kind === "term" && count("term") >= LIMITS.terms)
    return { refs, error: `一次最多引用 ${LIMITS.terms} 个术语。` };
  if (ref.kind === "passage" && count("passage") >= LIMITS.passages)
    next.splice(
      next.findIndex((r) => r.kind === "passage"),
      1,
    );
  if (ref.kind === "reading_item" && count("reading_item") >= LIMITS.reading_items)
    next.splice(
      next.findIndex((r) => r.kind === "reading_item"),
      1,
    );
  return { refs: [...next, ref] };
}

/** Removing a paper also removes its passage and reading-object references. */
export const removeRef = (refs: ChatRef[], ref: ChatRef) =>
  refs.filter(
    (r) =>
      refKey(r) !== refKey(ref) &&
      !(
        ref.kind === "paper" &&
        (r.kind === "passage" || r.kind === "reading_item") &&
        r.paper_id === ref.paper_id
      ),
  );

const clip = (text: string, max: number) =>
  text.length > max ? text.slice(0, max) : text;

/**
 * The conversation so far, as the service accepts it: answered turns only,
 * the latest twelve messages, each within the per-message limit.
 */
export function historyOf(messages: ChatMessage[]): ChatHistory[] {
  const out: ChatHistory[] = [];
  messages.forEach((m, i) => {
    const answer = messages[i + 1];
    if (m.role !== "user" || answer?.role !== "assistant" || answer.error) return;
    if (!m.content.trim() || !answer.content.trim()) return;
    out.push(
      { role: "user", content: clip(m.content, LIMITS.turn) },
      { role: "assistant", content: clip(answer.content, LIMITS.turn) },
    );
  });
  return out.slice(-LIMITS.history);
}

/**
 * Body of `POST /api/chat`. Papers missing from the library (deleted since)
 * are left out together with their sources; the chips show them as missing.
 */
export function requestFor(
  refs: ChatRef[],
  question: string,
  history: ChatHistory[],
  library?: Set<string> | null,
) {
  const ok = (id: string) => !library || library.has(id);
  const papers = [
    ...new Set(refs.flatMap((r) => (r.kind === "term" ? [] : [r.paper_id])).filter(ok)),
  ].slice(0, LIMITS.papers);
  const readingItems = refs
    .flatMap((r) =>
      r.kind === "reading_item" && papers.includes(r.paper_id)
        ? [{ paper_id: r.paper_id, item_id: r.item_id, source_version: r.source_version }]
        : [],
    )
    .slice(0, LIMITS.reading_items);
  return {
    question,
    history,
    papers,
    ...(readingItems.length ? { reading_items: readingItems } : {}),
    passages: refs.flatMap((r) =>
      r.kind === "passage" && papers.includes(r.paper_id)
        ? [
            {
              paper_id: r.paper_id,
              block_id: r.block_id,
              start: r.start,
              end: r.end,
              text: r.text,
            },
          ]
        : [],
    ),
    terms: refs.flatMap((r) =>
      r.kind === "term"
        ? [
            {
              term: clip(r.term, 80),
              explanation: clip(r.explanation, 1200),
              note: clip(r.note, 600),
              ...(r.paper_id && papers.includes(r.paper_id)
                ? { paper_id: r.paper_id }
                : {}),
            },
          ]
        : [],
    ),
  };
}

export function summarize(doc: Conversation): ConversationSummary {
  const last = [...doc.messages].reverse().find((m) => m.role === "user");
  return {
    id: doc.id,
    title: doc.title,
    created_at: doc.created_at,
    updated_at: doc.updated_at,
    pinned: doc.pinned,
    messages: doc.messages.length,
    paper_ids: doc.refs.flatMap((r) => (r.kind === "paper" ? [r.paper_id] : [])),
    preview: (last?.content || "").slice(0, 120),
  };
}

/** The same source-backed answer is exported from the thread and its answer menu. */
export function answerMarkdown(
  message: ChatMessage,
  paperTitles: ReadonlyMap<string, string> = new Map(),
): string {
  const lines = [message.error ? `（未完成：${message.error}）` : message.content, ""];
  const titles = new Map(paperTitles);
  for (const paper of message.reply?.papers || [])
    titles.set(paper.paper_id, paper.title);
  const quotes = message.reply?.quotes || [];
  if (quotes.length) {
    lines.push("依据：", "");
    quotes.forEach((q, i) => {
      const paper = titles.get(q.paper_id || "") || "";
      lines.push(
        markdownListItem(
          `${paper ? `《${paper}》` : ""}P${q.page}：“${q.quote}”`,
          `${q.n ?? i + 1}.`,
        ),
      );
    });
    lines.push("");
  }
  for (const warning of message.reply?.warnings || [])
    lines.push(markdownQuote(`提示：${warning}`), "");
  return lines.join("\n");
}

/** Readable notes: questions, answers, and the quoted source under each answer. */
export function conversationMarkdown(doc: Conversation): string {
  const title = doc.title || "未命名对话";
  const lines = [
    `# ${title}`,
    "",
    `> 导出自 PaperLens · ${doc.updated_at.slice(0, 10)}`,
    "",
  ];
  const papers = doc.refs.flatMap((r) => (r.kind === "paper" ? [`《${r.title}》`] : []));
  const terms = doc.refs.flatMap((r) => (r.kind === "term" ? [r.term] : []));
  if (papers.length) lines.push(`引用论文：${papers.join("、")}`, "");
  if (terms.length) lines.push(`引用术语：${terms.join("、")}`, "");
  const titles = new Map(
    doc.messages.flatMap((m) => m.reply?.papers?.map((p) => [p.paper_id, p.title]) || []),
  );
  for (const m of doc.messages) {
    if (m.role === "user") {
      lines.push("## 问", "", m.content, "");
      for (const r of m.refs)
        if (r.kind === "passage")
          lines.push(
            markdownQuote(
              `原文（${r.paper_title}${r.page ? ` P${r.page}` : ""}）：${r.text}`,
            ),
            "",
          );
        else if (r.kind === "reading_item")
          lines.push(
            markdownQuote(
              `${r.label || "阅读对象"}（${r.paper_title}${r.page ? ` P${r.page}` : ""}）：${r.text}`,
            ),
            "",
          );
      continue;
    }
    lines.push("## 答", "", answerMarkdown(m, titles), "");
  }
  return lines.join("\n");
}

/* ---------- where conversations are kept ---------- */

interface Keeper {
  list(): Promise<ConversationSummary[]>;
  get(id: string): Promise<Conversation | null>;
  save(doc: Conversation): Promise<string>;
  remove(id: string): Promise<void>;
}

const LOCAL_KEY = "paperlens.chats";
const readLocal = (): Conversation[] => {
  try {
    const raw = JSON.parse(localStorage.getItem(LOCAL_KEY) || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
};
const writeLocal = (docs: Conversation[]) =>
  localStorage.setItem(LOCAL_KEY, JSON.stringify(docs));

/** This browser only; used when the service has no conversation storage. */
const localKeeper: Keeper = {
  list: async () =>
    readLocal()
      .map(summarize)
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
  get: async (id) => readLocal().find((d) => d.id === id) || null,
  save: async (doc) => {
    const updated = new Date().toISOString();
    writeLocal([
      { ...doc, updated_at: updated },
      ...readLocal().filter((d) => d.id !== doc.id),
    ]);
    return updated;
  },
  remove: async (id) => writeLocal(readLocal().filter((d) => d.id !== id)),
};

const remoteKeeper: Keeper = {
  list: listConversations,
  get: (id) =>
    getConversation<Conversation>(id).catch((e) => {
      if (e?.status === 404) return null;
      throw e;
    }),
  save: async (doc) => (await putConversation(doc.id, doc)).updated_at,
  remove: deleteConversation,
};

let config = {
  /** `POST /api/chat` exists: several papers, passages and terms. */
  multi: false,
  stream: false,
  routing: false,
  /** `/api/conversations` exists. */
  remote: false,
  library: null as Set<string> | null,
};
const keeper = () => (config.remote ? remoteKeeper : localKeeper);

export function configureChat(next: Partial<typeof config>) {
  const moved = next.remote !== undefined && next.remote !== config.remote;
  config = { ...config, ...next };
  if (moved) void refreshList();
}
export const storedLocally = () => !config.remote;

/* ---------- live sessions ---------- */

export interface Pending {
  messageId: string;
  started: number;
  papers: number;
  controller: AbortController;
  /** Live provisional text; discarded unless the final reply validates its sources. */
  text?: string;
}
export interface Session {
  doc: Conversation;
  pending: Pending | null;
  /** Saved to conversation storage after each change; quick drafts use browser storage. */
  keep: boolean;
  saveError: string;
}

const sessions = new Map<string, Session>();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((notify) => notify());
const subscribe = (notify: () => void) => {
  listeners.add(notify);
  return () => listeners.delete(notify);
};
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const deleting = new Set<string>();
const saves = new Map<string, Set<Promise<string>>>();

function patch(key: string, next: Partial<Session>) {
  const old = sessions.get(key);
  if (!old) return;
  sessions.set(key, { ...old, ...next });
  emit();
}

export function useSession(key: string | null): Session | undefined {
  return useSyncExternalStore(subscribe, () => (key ? sessions.get(key) : undefined));
}
export const getSession = (key: string) => sessions.get(key);

/**
 * Opens (or reuses) a session held in memory. `notify: false` when called
 * while rendering: the caller reads the new session right away.
 */
export function ensureSession(
  key: string,
  make: () => Conversation,
  keep: boolean,
  notify = true,
) {
  if (!sessions.has(key)) {
    let doc = make();
    if (key.startsWith("quick:")) {
      try {
        const saved = JSON.parse(localStorage.getItem(`paperlens.${key}`) || "null");
        if (saved?.id && Array.isArray(saved.messages) && Array.isArray(saved.refs))
          doc = saved;
      } catch {
        /* browser storage is optional */
      }
    }
    sessions.set(key, { doc, pending: null, keep, saveError: "" });
    if (notify) emit();
  }
  return sessions.get(key)!;
}

/** Loads a saved conversation; an unknown id opens as a new, empty one. */
export async function openConversation(id: string): Promise<Session> {
  const open = sessions.get(id);
  if (open) return open;
  const doc = await keeper().get(id);
  return ensureSession(id, () => doc || { ...blankConversation(), id }, true);
}

function schedule(key: string) {
  const s = sessions.get(key);
  if (!s?.keep || !s.doc.messages.length || deleting.has(key)) return;
  clearTimeout(timers.get(key));
  timers.set(
    key,
    setTimeout(async () => {
      timers.delete(key);
      const current = sessions.get(key);
      if (!current || deleting.has(key)) return;
      const saving = keeper().save(current.doc);
      const pending = saves.get(key) || new Set<Promise<string>>();
      pending.add(saving);
      saves.set(key, pending);
      try {
        const updated_at = await saving;
        if (deleting.has(key) || !sessions.has(key)) return;
        const latest = sessions.get(key);
        if (latest) {
          sessions.set(key, {
            ...latest,
            doc: { ...latest.doc, updated_at },
            saveError: "",
          });
          emit();
        }
        upsertSummary(summarize({ ...current.doc, updated_at }));
      } catch (e) {
        if (!deleting.has(key))
          patch(key, { saveError: e instanceof Error ? e.message : String(e) });
      } finally {
        pending.delete(saving);
        if (!pending.size) saves.delete(key);
      }
    }, 400),
  );
}

export function updateDoc(key: string, change: (doc: Conversation) => Conversation) {
  const s = sessions.get(key);
  if (!s) return;
  const doc = change(s.doc);
  patch(key, { doc });
  if (key.startsWith("quick:")) {
    try {
      localStorage.setItem(`paperlens.${key}`, JSON.stringify(doc));
    } catch {
      /* optional */
    }
  }
  schedule(key);
}

export function setRefs(key: string, refs: ChatRef[]) {
  updateDoc(key, (doc) => ({ ...doc, refs }));
}

export const isAbort = (e: unknown) =>
  e instanceof DOMException
    ? e.name === "AbortError"
    : (e as Error)?.name === "AbortError";

async function ask(
  key: string,
  refs: ChatRef[],
  question: string,
  history: ChatHistory[],
  signal: AbortSignal,
  onText: (text: string) => void,
) {
  const body = {
    ...requestFor(refs, question, history, config.library),
    ...(config.routing
      ? {
          model_role: key.startsWith("quick:")
            ? ("chat" as const)
            : ("conversation" as const),
        }
      : {}),
  };
  if (!body.papers.length) throw new Error("请先引用至少一篇论文：输入 @ 或点 + 选择。");
  if (config.multi) {
    const submit = (request: Parameters<typeof askPapers>[0]) =>
      config.stream
        ? askPapersStream(request, signal, onText)
        : askPapers(request, signal);
    try {
      return await submit(body);
    } catch (error) {
      if (!(error instanceof ApiError) || !error.inputBudget || signal.aborted)
        throw error;
      const approval = confirmInputBudget(error.inputBudget, "本次提问");
      if (!approval || signal.aborted) throw error;
      // Only local preflight failures carry this detail; never repeat a request
      // whose model outcome is unknown or whose response was interrupted.
      return submit({ ...body, input_budget: approval });
    }
  }
  if (body.reading_items?.length)
    throw new Error("当前本地服务不支持引用公式、表格等阅读对象；请更新服务后重新提问。");
  if (body.papers.length > 1 || body.passages.length > 1)
    throw new Error("当前本地服务只支持对单篇论文提问；更新服务后可同时引用多篇论文。");
  const [passage] = body.passages;
  const reply = await askPaper(
    body.papers[0],
    {
      question,
      history,
      selection: passage && {
        block_id: passage.block_id,
        start: passage.start,
        end: passage.end,
        text: passage.text,
      },
    },
    signal,
  );
  // Single-paper replies carry no paper ids or [n] numbers.
  return {
    ...reply,
    quotes: reply.quotes?.map((q, i) => ({
      ...q,
      n: q.n ?? i + 1,
      paper_id: q.paper_id ?? body.papers[0],
    })),
  };
}

async function run(key: string, user: ChatMessage, history: ChatHistory[]) {
  const controller = new AbortController();
  const papers = requestFor(user.refs, user.content, history, config.library).papers
    .length;
  patch(key, {
    pending: { messageId: user.id, started: Date.now(), papers, controller },
  });
  let answer: ChatMessage;
  const base = { id: newId(), role: "assistant" as const, refs: [], created_at: "" };
  try {
    const reply = await ask(
      key,
      user.refs,
      user.content,
      history,
      controller.signal,
      (text) => {
        const current = sessions.get(key);
        if (current?.pending?.controller !== controller || controller.signal.aborted)
          return;
        patch(key, { pending: { ...current.pending, text } });
      },
    );
    answer = { ...base, content: reply.answer, reply, error: null };
  } catch (e) {
    answer = {
      ...base,
      content: "",
      reply: null,
      error: isAbort(e)
        ? "已停止生成；未核对的内容未保存。"
        : e instanceof Error
          ? e.message
          : String(e),
    };
  }
  answer.created_at = new Date().toISOString();
  const s = sessions.get(key);
  if (!s || s.pending?.controller !== controller) return;
  patch(key, { pending: null });
  updateDoc(key, (doc) => ({ ...doc, messages: [...doc.messages, answer] }));
}

export function send(key: string, question: string) {
  const s = sessions.get(key);
  const text = question.trim();
  if (!s || s.pending || !text) return;
  const user: ChatMessage = {
    id: newId(),
    role: "user",
    content: clip(text, LIMITS.turn),
    created_at: new Date().toISOString(),
    refs: s.doc.refs,
    reply: null,
    error: null,
  };
  const history = historyOf(s.doc.messages);
  updateDoc(key, (doc) => ({
    ...doc,
    title: doc.title || titleFrom(text),
    messages: [...doc.messages, user],
  }));
  void run(key, user, history);
}

/** Asks the question before `assistantId` again, with the references it had. */
export function retry(key: string, assistantId: string) {
  const s = sessions.get(key);
  if (!s || s.pending) return;
  const at = s.doc.messages.findIndex((m) => m.id === assistantId);
  const user = s.doc.messages[at - 1];
  if (at < 1 || user.role !== "user") return;
  const history = historyOf(s.doc.messages.slice(0, at - 1));
  updateDoc(key, (doc) => ({
    ...doc,
    messages: doc.messages.filter((m) => m.id !== assistantId),
  }));
  void run(key, user, history);
}

export function stop(key: string) {
  sessions.get(key)?.pending?.controller.abort();
}

/** Keeps a quick chat as a conversation; returns its id. */
export async function promote(key: string): Promise<string> {
  const s = sessions.get(key);
  if (!s || s.pending) throw new Error("请等回答完成后再保存。");
  const doc = s.doc;
  const updated_at = await keeper().save(doc);
  sessions.set(doc.id, { ...s, doc: { ...doc, updated_at }, keep: true });
  sessions.delete(key);
  try {
    localStorage.removeItem(`paperlens.${key}`);
  } catch {
    /* optional */
  }
  emit();
  upsertSummary(summarize({ ...doc, updated_at }));
  return doc.id;
}

/** Forgets a quick chat so the next one starts fresh. */
export function discard(key: string) {
  stop(key);
  sessions.delete(key);
  try {
    localStorage.removeItem(`paperlens.${key}`);
  } catch {
    /* optional */
  }
  emit();
}

/* ---------- the list on the 对话 page ---------- */

let list: { items: ConversationSummary[] | null; error: string } = {
  items: null,
  error: "",
};
const listListeners = new Set<() => void>();
const setList = (next: typeof list) => {
  list = next;
  listListeners.forEach((notify) => notify());
};
export function useConversationList() {
  return useSyncExternalStore(
    (notify) => {
      listListeners.add(notify);
      return () => listListeners.delete(notify);
    },
    () => list,
  );
}
export async function refreshList() {
  try {
    setList({ items: await keeper().list(), error: "" });
  } catch (e) {
    setList({ items: list.items, error: e instanceof Error ? e.message : String(e) });
  }
}
function upsertSummary(summary: ConversationSummary) {
  if (!list.items) return;
  setList({
    ...list,
    items: [summary, ...list.items.filter((c) => c.id !== summary.id)].sort((a, b) =>
      b.updated_at.localeCompare(a.updated_at),
    ),
  });
}

/** Removes a conversation; `restore` puts it back (undo). */
export async function removeConversation(id: string) {
  if (deleting.has(id)) throw new Error("正在删除这段对话，请稍候。");
  deleting.add(id);
  const storage = keeper();
  try {
    stop(id);
    clearTimeout(timers.get(id));
    timers.delete(id);
    // A late autosave must complete before DELETE, or it could recreate the file.
    await Promise.allSettled([...(saves.get(id) || [])]);
    const doc = sessions.get(id)?.doc || (await storage.get(id));
    await storage.remove(id);
    if (doc) rememberDeletedConversation(doc);
    sessions.delete(id);
    emit();
    if (list.items) setList({ ...list, items: list.items.filter((c) => c.id !== id) });
    return doc;
  } catch (error) {
    deleting.delete(id);
    schedule(id);
    throw error;
  } finally {
    deleting.delete(id);
  }
}
export async function restoreConversation(doc: Conversation) {
  const updated_at = await keeper().save(doc);
  sessions.set(doc.id, {
    doc: { ...doc, updated_at },
    pending: null,
    keep: true,
    saveError: "",
  });
  emit();
  upsertSummary(summarize({ ...doc, updated_at }));
  forgetDeletedConversation(doc.id);
}

export async function setPinned(id: string, pinned: boolean) {
  const s = sessions.get(id) || (await openConversation(id));
  updateDoc(id, (doc) => ({ ...doc, pinned }));
  if (!s.doc.messages.length) return;
  if (list.items)
    setList({
      ...list,
      items: list.items.map((c) => (c.id === id ? { ...c, pinned } : c)),
    });
}

/**
 * Starting a conversation elsewhere (the reader, the term library): the refs
 * wait here until the 对话 page opens a new conversation.
 */
let seed: ChatRef[] | null = null;
export function startWith(refs: ChatRef[]) {
  seed = refs;
}
export function takeSeed(): ChatRef[] | null {
  const refs = seed;
  seed = null;
  return refs;
}
