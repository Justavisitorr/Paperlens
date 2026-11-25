import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { TexText } from "./AnalysisText";
import { Icon, type IconName } from "./icons";
import { downloadFile } from "./export";
import { Markdown } from "./markdown";
import { go, parseRoute } from "./router";
import { jumpTo } from "./ReaderViews";
import { jumpLater, routePaperId, useTerms } from "./terms";
import { useLoadedAnnotations } from "./annotations";
import {
  addRef,
  answerMarkdown,
  droppedPassage,
  PASSAGE_DRAG_TYPE,
  LIMITS,
  refKey,
  removeRef,
  type ChatMessage,
  type ChatRef,
  type Session,
} from "./chat";
import type { ChatQuote, PaperSummary } from "./api";

/** Opens a quoted block in the full text, from anywhere in the workspace. */
export function openSource(paperId: string, blockId: string) {
  const route = parseRoute(location.hash);
  const here =
    route?.page === "read" &&
    route.paperId === paperId &&
    route.mode === "text" &&
    document.getElementById(`block-${blockId}`);
  if (here) return jumpTo(`block-${blockId}`);
  jumpLater("text", blockId);
  go({ page: "read", paperId, mode: "text" });
}

/** Reading objects have their own anchors, independent of raw block offsets. */
export function openReadingItem(paperId: string, itemId: string) {
  const route = parseRoute(location.hash);
  if (route?.page === "read" && route.paperId === paperId && route.mode === "text") {
    if (document.getElementById(itemId)) return jumpTo(itemId);
  }
  try {
    sessionStorage.setItem(
      "paperlens:pending-reading-item",
      JSON.stringify({ paperId, itemId }),
    );
  } catch {
    /* Navigation still works when browser storage is unavailable. */
  }
  go({ page: "read", paperId, mode: "text" });
}

const cut = (text: string, n: number) =>
  text.length > n ? `${text.slice(0, n)}…` : text;

/* ---------- reference chips ---------- */

const refIcon: Record<ChatRef["kind"], IconName> = {
  paper: "file",
  term: "bookmark",
  passage: "quote",
  reading_item: "quote",
};
export function refLabel(ref: ChatRef) {
  return ref.kind === "paper"
    ? ref.title
    : ref.kind === "term"
      ? ref.term
      : ref.kind === "reading_item"
        ? ref.label || "阅读对象"
        : `“${cut(ref.text, 18)}”`;
}
function refTitle(ref: ChatRef) {
  if (ref.kind === "paper") return `论文：${ref.title}`;
  if (ref.kind === "term")
    return `术语：${ref.term}${ref.note ? `\n笔记：${ref.note}` : ""}${ref.explanation ? `\n${cut(ref.explanation, 200)}` : ""}`;
  if (ref.kind === "reading_item")
    return `${ref.label || "阅读对象"}${ref.page ? `（P${ref.page}）` : ""}：${ref.text}\n来自《${ref.paper_title}》`;
  return `原文${ref.page ? `（P${ref.page}）` : ""}：${ref.text}\n来自《${ref.paper_title}》`;
}

export function RefChip({
  value,
  missing,
  locked,
  onRemove,
  onOpen,
}: {
  value: ChatRef;
  missing?: boolean;
  locked?: boolean;
  onRemove?: () => void;
  onOpen?: () => void;
}) {
  return (
    <span
      className={`ref-chip kind-${value.kind} ${missing ? "is-missing" : ""}`}
      title={missing ? "这篇论文已不在文献库中，提问时会跳过" : refTitle(value)}
    >
      <button type="button" className="ref-chip-main" onClick={onOpen} disabled={!onOpen}>
        <Icon name={refIcon[value.kind]} size={13} />
        <span>{refLabel(value)}</span>
      </button>
      {onRemove && !locked && (
        <button
          type="button"
          className="ref-chip-x"
          aria-label={`移除引用 ${refLabel(value)}`}
          onClick={onRemove}
        >
          <Icon name="close" size={12} />
        </button>
      )}
    </span>
  );
}

/* ---------- the thread ---------- */

function useSeconds(since: number | undefined) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!since) return;
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [since]);
  return since ? Math.max(0, Math.round((Date.now() - since) / 1000)) : 0;
}

interface Tip {
  quote: ChatQuote;
  paper: string;
  x: number;
  top: number;
  bottom: number;
}

export function Thread({
  session,
  papers,
  empty,
  onRetry,
  onStop,
  onPin,
}: {
  session: Session;
  papers: PaperSummary[];
  /** Shown before the first question. */
  empty?: ReactNode;
  onRetry: (assistantId: string) => void;
  onStop: () => void;
  onPin?: (
    answer: ChatMessage,
    question: ChatMessage | undefined,
  ) => Promise<void> | void;
}) {
  const { doc, pending } = session;
  const scroller = useRef<HTMLDivElement>(null);
  const [tip, setTip] = useState<Tip | null>(null);
  const seconds = useSeconds(pending?.started);
  const titles = useMemo(() => {
    const out = new Map<string, string>();
    for (const r of doc.refs) if (r.kind === "paper") out.set(r.paper_id, r.title);
    for (const m of doc.messages)
      for (const p of m.reply?.papers || []) out.set(p.paper_id, p.title);
    for (const p of papers) out.set(p.id, p.title);
    return out;
  }, [doc, papers]);

  // Follow the conversation when it grows, unless the reader scrolled up to reread.
  const count = doc.messages.length + (pending ? 1 : 0);
  const near = useRef(true);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const mine = doc.messages[doc.messages.length - 1]?.role === "user";
    if (near.current || mine) el.scrollTop = el.scrollHeight;
  }, [count, doc.messages, pending?.text]);

  const lastAnswer = [...doc.messages].reverse().find((m) => m.role === "assistant");
  return (
    <div
      className="chat-thread"
      ref={scroller}
      onScroll={(e) => {
        const el = e.currentTarget;
        near.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        setTip(null);
      }}
      aria-live="polite"
      aria-busy={Boolean(pending)}
    >
      {!doc.messages.length && !pending && empty}
      {doc.messages.map((m, index) =>
        m.role === "user" ? (
          <UserTurn key={m.id} message={m} />
        ) : (
          <Answer
            key={m.id}
            message={m}
            titles={titles}
            last={m === lastAnswer && !pending}
            onRetry={() => onRetry(m.id)}
            onTip={setTip}
            onPin={
              onPin
                ? () =>
                    onPin(
                      m,
                      [...doc.messages.slice(0, index)]
                        .reverse()
                        .find((turn) => turn.role === "user"),
                    )
                : undefined
            }
          />
        ),
      )}
      {pending && (
        <div className="chat-turn is-assistant is-pending">
          <div className="answer-card">
            {pending.text && <div className="stream-preview">{pending.text}</div>}
            <div className="thinking">
              <span className="thinking-dots" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              {pending.text
                ? "正在生成 · 引文尚未核对"
                : `正在阅读 ${pending.papers || 1} 篇论文的原文并核对引文`}
              <small>{seconds} 秒</small>
            </div>
            <button
              className="text-button"
              onClick={onStop}
              title="只是不再等待；已发出的请求仍可能计入用量"
            >
              <Icon name="stop" size={13} />
              停止生成
            </button>
          </div>
        </div>
      )}
      {tip && <CiteTip tip={tip} />}
    </div>
  );
}

function UserTurn({ message }: { message: ChatMessage }) {
  const passages = message.refs.filter(
    (r) => r.kind === "passage" || r.kind === "reading_item",
  );
  const papers = message.refs.filter((r) => r.kind === "paper").length;
  const terms = message.refs.filter((r) => r.kind === "term").length;
  return (
    <div className="chat-turn is-user">
      <div className="user-bubble">
        {passages.map((r) => (
          <blockquote key={refKey(r)} className="bubble-quote">
            {r.kind === "reading_item" && <strong>{r.label || "阅读对象"} · </strong>}
            {cut(r.text, 160)}
          </blockquote>
        ))}
        <p>
          <TexText text={message.content} />
        </p>
      </div>
      {(papers > 1 || terms > 0) && (
        <small className="turn-meta">
          {[papers > 1 && `${papers} 篇论文`, terms > 0 && `${terms} 个术语`]
            .filter(Boolean)
            .join(" · ")}
        </small>
      )}
    </div>
  );
}

function Answer({
  message,
  titles,
  last,
  onRetry,
  onTip,
  onPin,
}: {
  message: ChatMessage;
  titles: Map<string, string>;
  last: boolean;
  onRetry: () => void;
  onTip: (tip: Tip | null) => void;
  onPin?: () => Promise<void> | void;
}) {
  const [copied, setCopied] = useState(false);
  const [pinState, setPinState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [pinError, setPinError] = useState("");
  const pinLocked = useRef(false);
  const reply = message.reply;
  const quotes = reply?.quotes || [];
  const byNumber = new Map(quotes.map((q, i) => [q.n ?? i + 1, q]));
  const groups = useMemo(() => {
    const out = new Map<string, ChatQuote[]>();
    for (const q of quotes) {
      const key = q.paper_id || "";
      out.set(key, [...(out.get(key) || []), q]);
    }
    return [...out];
  }, [quotes]);
  if (message.error)
    return (
      <div className="chat-turn is-assistant">
        <div className="answer-card is-error">
          <p className="error-line">
            <Icon name="warning" size={14} />
            {message.error}
          </p>
          {last && (
            <button className="button small" onClick={onRetry}>
              <Icon name="refresh" size={13} />
              重试
            </button>
          )}
        </div>
      </div>
    );

  const open = (q: ChatQuote) => q.paper_id && openSource(q.paper_id, q.block_id);
  const cite = (numbers: number[]) => (
    <span className="cites">
      {numbers.map((n) => {
        const q = byNumber.get(n);
        if (!q) return <span key={n}>[{n}]</span>;
        const show = (el: HTMLElement) => {
          const r = el.getBoundingClientRect();
          onTip({
            quote: q,
            paper: titles.get(q.paper_id || "") || "",
            x: r.left + r.width / 2,
            top: r.top,
            bottom: r.bottom,
          });
        };
        return (
          <button
            key={n}
            className="cite"
            aria-label={`依据 ${n}：第 ${q.page} 页`}
            onMouseEnter={(e) => show(e.currentTarget)}
            onFocus={(e) => show(e.currentTarget)}
            onMouseLeave={() => onTip(null)}
            onBlur={() => onTip(null)}
            onClick={() => open(q)}
          >
            {n}
          </button>
        );
      })}
    </span>
  );
  const copy = async () => {
    const sources = quotes.map(
      (q, i) =>
        `[${q.n ?? i + 1}] ${titles.get(q.paper_id || "") ? `《${titles.get(q.paper_id || "")}》` : ""}P${q.page}：“${q.quote}”`,
    );
    try {
      await navigator.clipboard.writeText(
        [message.content, ...(sources.length ? ["", ...sources] : [])].join("\n"),
      );
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard may be unavailable (file:// or denied) */
    }
  };
  const grounded = reply?.grounded !== false;
  const pin = async () => {
    if (!onPin || pinLocked.current) return;
    pinLocked.current = true;
    setPinState("saving");
    setPinError("");
    try {
      await onPin();
      setPinState("saved");
    } catch (e) {
      pinLocked.current = false;
      setPinState("error");
      setPinError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <div className="chat-turn is-assistant">
      <div className="answer-card">
        <Markdown text={message.content} cite={quotes.length ? cite : undefined} />
        {!!reply?.warnings?.length && (
          <details className="answer-sources" open={!grounded}>
            <summary>
              <Icon name={grounded ? "check" : "warning"} size={13} />
              {grounded ? "回答说明" : "回答需核对"}
            </summary>
            {reply.warnings.map((w, i) => (
              <p className={`answer-note ${grounded ? "" : "is-warn"}`} key={i}>
                <Icon name={grounded ? "check" : "warning"} size={13} />
                {w}
              </p>
            ))}
          </details>
        )}
        {groups.length > 0 && (
          <details className="answer-sources">
            <summary>
              <Icon name="quote" size={13} />
              依据 · {quotes.length} 处原文
              {groups.length > 1 && ` · ${groups.length} 篇论文`}
            </summary>
            {groups.map(([paperId, list]) => (
              <section key={paperId}>
                {(groups.length > 1 || !titles.has(paperId)) && (
                  <h6>{titles.get(paperId) || "论文原文"}</h6>
                )}
                <ol>
                  {list.map((q, i) => (
                    <li key={i}>
                      <button onClick={() => open(q)} title="在原文中查看">
                        <span className="source-n">{q.n ?? i + 1}</span>
                        <span className="page-badge">P{q.page}</span>
                        <span className="source-quote">{q.quote}</span>
                      </button>
                    </li>
                  ))}
                </ol>
              </section>
            ))}
          </details>
        )}
        <div className="answer-actions">
          <button className="text-button" onClick={copy}>
            <Icon name={copied ? "check" : "copy"} size={13} />
            {copied ? "已复制" : "复制"}
          </button>
          {onPin && reply && grounded && message.content.trim() && (
            <button
              className="text-button"
              onClick={() => void pin()}
              disabled={pinState === "saving" || pinState === "saved"}
            >
              <Icon name={pinState === "saved" ? "check" : "bookmark"} size={13} />
              {pinState === "saving"
                ? "正在放到页边…"
                : pinState === "saved"
                  ? "已放到页边"
                  : "放到页边"}
            </button>
          )}
          <button
            className="text-button"
            onClick={() =>
              downloadFile(
                answerMarkdown(message, titles),
                `回答-${message.id.slice(0, 8)}.md`,
                "text/markdown;charset=utf-8",
              )
            }
          >
            导出回答
          </button>
          {last && (
            <button
              className="text-button"
              onClick={onRetry}
              title="用同样的问题和引用再问一次"
            >
              <Icon name="refresh" size={13} />
              重新回答
            </button>
          )}
          {reply?.model && <small>{reply.model}</small>}
        </div>
        {pinState === "error" && (
          <p className="error-line" role="alert">
            未能放到页边：{pinError}
          </p>
        )}
      </div>
    </div>
  );
}

function CiteTip({ tip }: { tip: Tip }) {
  const box = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const vw = document.documentElement.clientWidth;
    const up = tip.top - h - 10 > 8;
    el.style.left = `${Math.min(Math.max(tip.x - w / 2, 8), vw - w - 8)}px`;
    el.style.top = `${up ? tip.top - h - 8 : tip.bottom + 8}px`;
    el.style.visibility = "visible";
  }, [tip]);
  return (
    <div className="cite-tip" ref={box} role="tooltip">
      <small>
        {tip.paper ? `${cut(tip.paper, 30)} · ` : ""}第 {tip.quote.page} 页
        {tip.quote.section ? ` · ${cut(tip.quote.section, 16)}` : ""}
      </small>
      <p>{cut(tip.quote.quote, 280)}</p>
      <em>点击在原文中查看</em>
    </div>
  );
}

/* ---------- suggestions ---------- */

export function Suggestions({
  refs,
  onPick,
}: {
  refs: ChatRef[];
  onPick: (question: string) => void;
}) {
  const papers = refs.filter((r) => r.kind === "paper").length;
  const passage = refs.some((r) => r.kind === "passage");
  const readingItem = refs.some((r) => r.kind === "reading_item");
  const terms = refs.some((r) => r.kind === "term");
  const list = [
    ...(readingItem ? [["解释图式", "解释所引图表或公式，联系上下文。"]] : []),
    ...(passage
      ? [
          ["解释这段", "用通俗的话解释引用的这段原文"],
          ["前后联系", "这段话在全文论证中起什么作用？"],
        ]
      : []),
    ...(terms ? [["检查理解", "结合术语笔记，检查我的理解。"]] : []),
    ...(papers > 1
      ? [
          ["比较方法", "比较这几篇论文的研究问题和方法"],
          ["比较结论", "它们的结论有没有互相矛盾的地方？"],
          ["比较实验", "哪篇论文的实验设置更严格？"],
        ]
      : papers === 1
        ? [
            ["研究问题", "这篇论文要解决什么问题？"],
            ["方法步骤", "方法的核心步骤是什么？"],
            ["研究不足", "作者承认了哪些局限？"],
          ]
        : []),
  ].slice(0, 4);
  if (!list.length) return null;
  return (
    <div className="suggestions">
      {list.map(([label, q]) => (
        <button key={label} title={q} onClick={() => onPick(q)}>
          <Icon name="sparkle" size={13} />
          {label}
        </button>
      ))}
    </div>
  );
}

/* ---------- composer with @ references ---------- */

interface PickItem {
  ref: ChatRef;
  group: "论文" | "我的术语" | "我的勾画";
  title: string;
  meta: string;
}

const drafts = new Map<string, string>();
const readDraft = (key: string) => {
  try {
    return drafts.get(key) ?? localStorage.getItem(`paperlens.draft:${key}`) ?? "";
  } catch {
    return drafts.get(key) || "";
  }
};
const COLORS = { yellow: "黄色", green: "绿色", blue: "蓝色", pink: "粉色" } as const;
const MENTION = /(^|\s)@([^\s@]{0,24})$/;

export function Composer({
  draftKey,
  refs,
  onRefs,
  papers,
  locked,
  pending,
  canHighlights,
  onSend,
  onStop,
  autoFocus,
  placeholder = "问点什么…",
  hint,
}: {
  /** Keeps unsent text per conversation. */
  draftKey: string;
  refs: ChatRef[];
  onRefs: (refs: ChatRef[]) => void;
  papers: PaperSummary[];
  /** A paper that stays referenced (the one being read). */
  locked?: string;
  pending: boolean;
  canHighlights: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  autoFocus?: boolean;
  placeholder?: string;
  hint?: ReactNode;
}) {
  const [text, setText] = useState(() => readDraft(draftKey));
  const [picker, setPicker] = useState<{ query: string; index: number } | null>(null);
  const [notice, setNotice] = useState("");
  const [colorPicker, setColorPicker] = useState(false);
  const [color, setColor] = useState<"all" | keyof typeof COLORS>("all");
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const loadedDraft = useRef(draftKey);
  const terms = useTerms();
  const library = useMemo(() => new Set(papers.map((p) => p.id)), [papers]);
  const paperIds = refs.flatMap((r) => (r.kind === "paper" ? [r.paper_id] : []));
  const highlights = useLoadedAnnotations(
    paperIds,
    canHighlights && (Boolean(picker) || colorPicker),
  );

  useEffect(() => {
    if (loadedDraft.current !== draftKey) return;
    drafts.set(draftKey, text);
    try {
      localStorage.setItem(`paperlens.draft:${draftKey}`, text);
    } catch {
      /* optional */
    }
  }, [draftKey, text]);
  useEffect(() => {
    loadedDraft.current = draftKey;
    setText(readDraft(draftKey));
  }, [draftKey]);
  useEffect(() => {
    if (autoFocus) input.current?.focus({ preventScroll: true });
  }, [autoFocus, draftKey]);
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), 4000);
    return () => clearTimeout(timer);
  }, [notice]);

  const items = useMemo((): PickItem[] => {
    if (!picker) return [];
    const q = picker.query.toLocaleLowerCase();
    const match = (...parts: string[]) =>
      !q || parts.join(" ").toLocaleLowerCase().includes(q);
    const titleOf = (id: string) =>
      papers.find((p) => p.id === id)?.title ||
      refs.find(
        (r): r is Extract<ChatRef, { kind: "paper" }> =>
          r.kind === "paper" && r.paper_id === id,
      )?.title ||
      "论文";
    const paperItems: PickItem[] = papers
      .filter((p) => p.status !== "failed" && match(p.title, p.source_name || ""))
      .slice(0, q ? 12 : 8)
      .map((p) => ({
        ref: { kind: "paper", paper_id: p.id, title: p.title },
        group: "论文",
        title: p.title,
        meta: p.pages ? `${p.pages} 页` : "",
      }));
    const termItems: PickItem[] = terms
      .filter((t) => match(t.term, t.note || "", t.explanation))
      .slice(0, q ? 12 : 6)
      .map((t) => {
        const id = routePaperId(t.paperId);
        return {
          ref: {
            kind: "term",
            term_id: t.id,
            term: t.term,
            explanation: t.explanation,
            note: t.note || "",
            ...(library.has(id) ? { paper_id: id } : {}),
            paper_title: t.paperTitle,
          },
          group: "我的术语",
          title: t.term,
          meta: t.note ? `笔记：${cut(t.note, 24)}` : cut(t.paperTitle, 24),
        };
      });
    const markItems: PickItem[] = highlights
      .filter(
        ({ a }) =>
          match(a.exact, a.note) && (color === "all" || (a.color || "yellow") === color),
      )
      .slice(0, q ? 12 : 6)
      .map(({ paperId, a }) => ({
        ref: {
          kind: "passage",
          paper_id: paperId,
          paper_title: titleOf(paperId),
          block_id: a.selection.block_id,
          start: a.selection.start,
          end: a.selection.end,
          text: a.selection.text,
          page: a.page,
          annotation_id: a.id,
        },
        group: "我的勾画",
        title: `“${cut(a.exact, 40)}”`,
        meta: `${COLORS[a.color || "yellow"]} · P${a.page}${a.note ? ` · ${cut(a.note, 20)}` : ""}`,
      }));
    return [...paperItems, ...termItems, ...markItems];
  }, [picker, papers, terms, highlights, library, refs, color]);
  const chosen = new Set(refs.map(refKey));

  const readMention = (value: string, caret: number) => {
    const m = MENTION.exec(value.slice(0, caret));
    setPicker((old) =>
      m ? { query: m[2], index: old?.query === m[2] ? old.index : 0 } : null,
    );
  };
  const pick = (item: PickItem) => {
    const el = input.current;
    const caret = el?.selectionStart ?? text.length;
    const before = text.slice(0, caret).replace(/@[^\s@]{0,24}$/, "");
    const next = before + text.slice(caret);
    setText(next);
    setPicker(null);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(before.length, before.length);
    });
    const key = refKey(item.ref);
    if (chosen.has(key)) {
      if (item.ref.kind === "paper" && item.ref.paper_id === locked) return;
      onRefs(removeRef(refs, item.ref));
      return;
    }
    const added = addRef(refs, item.ref);
    if (added.error) setNotice(added.error);
    onRefs(added.refs);
  };
  const openPicker = () => {
    const el = input.current;
    if (!el) return;
    const caret = el.selectionStart ?? text.length;
    const pad = caret > 0 && !/\s/.test(text[caret - 1]) ? " @" : "@";
    const next = text.slice(0, caret) + pad + text.slice(caret);
    setText(next);
    setPicker({ query: "", index: 0 });
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(caret + pad.length, caret + pad.length);
    });
  };
  const submit = () => {
    if (pending || !text.trim()) return;
    onSend(text);
    setText("");
    setPicker(null);
  };
  const onKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (picker && items.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        setPicker({
          ...picker,
          index: (picker.index + step + items.length) % items.length,
        });
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pick(items[Math.min(picker.index, items.length - 1)]);
        return;
      }
    }
    if (picker && e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setPicker(null);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const addColor = () => {
    let next = refs;
    let count = 0;
    const eligible = highlights.filter(
      ({ a }) => color === "all" || (a.color || "yellow") === color,
    );
    for (const { paperId, a } of eligible) {
      if (next.filter((r) => r.kind === "passage").length >= LIMITS.passages) break;
      const result = addRef(next, {
        kind: "passage",
        paper_id: paperId,
        paper_title: papers.find((p) => p.id === paperId)?.title || "论文",
        block_id: a.selection.block_id,
        start: a.selection.start,
        end: a.selection.end,
        text: a.selection.text,
        page: a.page,
        annotation_id: a.id,
      });
      if (result.refs !== next) count++;
      next = result.refs;
    }
    onRefs(next);
    setNotice(
      count
        ? `已加入 ${count} 处原文（一次最多 ${LIMITS.passages} 处）`
        : "没有可加入的勾画，或原文引用已达到上限。",
    );
    setColorPicker(false);
  };
  let group = "";
  return (
    <div
      className={`composer ${pending ? "is-pending" : ""} ${dragging ? "is-dragging" : ""}`}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes(PASSAGE_DRAG_TYPE)) {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
          setDragging(true);
        }
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false);
      }}
      onDrop={(e) => {
        setDragging(false);
        if (!e.dataTransfer.types.includes(PASSAGE_DRAG_TYPE)) return;
        e.preventDefault();
        const passage = droppedPassage(e.dataTransfer.getData(PASSAGE_DRAG_TYPE));
        if (!passage || !library.has(passage.paper_id)) {
          setNotice("这段原文的来源信息不完整，请重新选取。");
          return;
        }
        const result = addRef(refs, passage);
        onRefs(result.refs);
        setNotice(result.error || "已引用这段原文；发送前可继续添加其他段落。");
        input.current?.focus();
      }}
    >
      {colorPicker && (
        <div className="color-ref-picker popover">
          <label>
            按勾画颜色引用{" "}
            <select
              value={color}
              onChange={(e) => setColor(e.target.value as typeof color)}
            >
              <option value="all">全部颜色</option>
              {Object.entries(COLORS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <p>
            当前引用论文中有{" "}
            {
              highlights.filter(
                ({ a }) => color === "all" || (a.color || "yellow") === color,
              ).length
            }{" "}
            处匹配勾画；一次最多引用 {LIMITS.passages} 处。
          </p>
          <button className="button small" onClick={addColor}>
            加入勾画
          </button>
          <button className="text-button" onClick={() => setColorPicker(false)}>
            关闭
          </button>
        </div>
      )}
      {picker && (
        <div
          className="ref-picker popover"
          role="listbox"
          aria-label="引用论文、术语或勾画"
        >
          {items.length ? (
            items.map((item, i) => {
              const head = item.group !== group;
              group = item.group;
              const on = chosen.has(refKey(item.ref));
              return (
                <div key={refKey(item.ref)}>
                  {head && <div className="group-label">{item.group}</div>}
                  <button
                    type="button"
                    role="option"
                    aria-selected={i === picker.index}
                    className={`pick-row ${i === picker.index ? "is-active" : ""} ${on ? "is-on" : ""}`}
                    onMouseDown={(e) => e.preventDefault()}
                    onMouseEnter={() => setPicker({ ...picker, index: i })}
                    onClick={() => pick(item)}
                  >
                    <Icon name={refIcon[item.ref.kind]} size={14} />
                    <span>
                      <strong>{item.title}</strong>
                      {item.meta && <small>{item.meta}</small>}
                    </span>
                    {on && <Icon name="check" size={14} />}
                  </button>
                </div>
              );
            })
          ) : (
            <p className="empty-line">
              {picker.query
                ? `没有与“${picker.query}”匹配的论文、术语或勾画。`
                : "文献库和术语库都还是空的。"}
            </p>
          )}
          <p className="picker-foot">
            ↑↓ 选择 · Enter 引用 · 再选一次取消
            {canHighlights && !paperIds.length && " · 引用论文后可选其中的勾画"}
          </p>
        </div>
      )}
      {refs.length > 0 && (
        <div className="composer-refs" aria-label="本次对话引用">
          {refs.map((r) => (
            <RefChip
              key={refKey(r)}
              value={r}
              missing={r.kind !== "term" && !library.has(r.paper_id)}
              locked={r.kind === "paper" && r.paper_id === locked}
              onRemove={() => onRefs(removeRef(refs, r))}
              onOpen={
                r.kind === "passage"
                  ? () => openSource(r.paper_id, r.block_id)
                  : r.kind === "reading_item" && library.has(r.paper_id)
                    ? () => openReadingItem(r.paper_id, r.item_id)
                    : r.kind === "paper" && library.has(r.paper_id)
                      ? () => go({ page: "read", paperId: r.paper_id, mode: "map" })
                      : undefined
              }
            />
          ))}
        </div>
      )}
      <textarea
        ref={input}
        rows={1}
        value={text}
        placeholder={placeholder}
        aria-label="输入问题"
        onChange={(e) => {
          setText(e.target.value);
          readMention(e.target.value, e.target.selectionStart);
        }}
        onKeyDown={onKey}
        onKeyUp={(e) => {
          if (e.key === "ArrowLeft" || e.key === "ArrowRight")
            readMention(e.currentTarget.value, e.currentTarget.selectionStart);
        }}
        onBlur={() => setPicker(null)}
      />
      <div className="composer-bar">
        <button
          type="button"
          className="icon-button small"
          title="引用论文、术语或勾画（@）"
          aria-label="添加引用"
          onMouseDown={(e) => e.preventDefault()}
          onClick={openPicker}
        >
          <Icon name="at" size={16} />
        </button>
        {canHighlights && paperIds.length > 0 && (
          <button
            type="button"
            className="text-button"
            aria-expanded={colorPicker}
            onClick={() => {
              setColorPicker(!colorPicker);
              setPicker(null);
            }}
          >
            勾画 ▾
          </button>
        )}
        <span className="composer-hint">
          {notice || hint || "@ 引用 · Enter 发送 · Shift Enter 换行"}
        </span>
        {pending ? (
          <button
            type="button"
            className="send-button is-stop"
            onClick={onStop}
            aria-label="停止生成"
            title="停止等待（已发出的请求仍可能计入用量）"
          >
            <Icon name="stop" size={15} />
          </button>
        ) : (
          <button
            type="button"
            className="send-button"
            onClick={submit}
            disabled={!text.trim()}
            aria-label="发送"
            title="发送（Enter）"
          >
            <Icon name="send" size={15} />
          </button>
        )}
      </div>
    </div>
  );
}
