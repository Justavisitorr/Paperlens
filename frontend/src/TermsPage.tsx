import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./icons";
import { go, href } from "./router";
import { downloadFile } from "./export";
import { askPaper, askPapers, type PaperSummary } from "./api";
import { AnalysisText, TexText } from "./AnalysisText";
import { Markdown } from "./markdown";
import { startWith } from "./chat";
import {
  explainQuestion,
  explanationLabel,
  jumpLater,
  removeTerms,
  restoreTerms,
  routePaperId,
  termsCsv,
  termsMarkdown,
  updateTerm,
  useTerms,
  type SavedTerm,
} from "./terms";

const day = (iso: string) =>
  new Date(iso).toLocaleDateString("zh-CN", { month: "short", day: "numeric" });

/** Which chat endpoint the service offers; null offline or on old services. */
export type TermChat = "papers" | "paper" | null;
type Filter = "all" | "todo" | "noted";

/** “术语本”: terms kept from every paper, beside the paper list. */
export function TermsPage({
  papers,
  chat = null,
}: {
  papers: PaperSummary[];
  chat?: TermChat;
}) {
  const terms = useTerms();
  const [query, setQuery] = useState("");
  const [paper, setPaper] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<"recent" | "name">("recent");
  const [exportsOpen, setExportsOpen] = useState(false);
  const [undo, setUndo] = useState<SavedTerm[] | null>(null);
  const exportRef = useRef<HTMLDivElement>(null);

  const sources = useMemo(() => {
    const out = new Map<string, { title: string; count: number }>();
    for (const t of terms) {
      const s = out.get(t.paperId);
      if (s) s.count++;
      else out.set(t.paperId, { title: t.paperTitle, count: 1 });
    }
    return [...out];
  }, [terms]);
  const todo = terms.filter((t) => !t.explanation.trim()).length;
  const noted = terms.filter((t) => t.note).length;
  const activePaper = sources.some(([id]) => id === paper) ? paper : "";
  const q = query.trim().toLocaleLowerCase();
  const shown = useMemo(() => {
    const list = terms.filter(
      (t) =>
        (!activePaper || t.paperId === activePaper) &&
        (filter === "all" ||
          (filter === "todo" ? !t.explanation.trim() : Boolean(t.note))) &&
        (!q ||
          `${t.term} ${t.explanation} ${t.note || ""} ${t.quote || ""}`
            .toLocaleLowerCase()
            .includes(q)),
    );
    return sort === "name"
      ? [...list].sort((a, b) => a.term.localeCompare(b.term, "zh-CN"))
      : list;
  }, [terms, activePaper, filter, q, sort]);

  useEffect(() => {
    if (!exportsOpen) return;
    const away = (e: PointerEvent) => {
      if (!exportRef.current?.contains(e.target as Node)) setExportsOpen(false);
    };
    const key = (e: KeyboardEvent) => e.key === "Escape" && setExportsOpen(false);
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", key);
    };
  }, [exportsOpen]);
  useEffect(() => {
    if (!undo) return;
    const timer = setTimeout(() => setUndo(null), 6000);
    return () => clearTimeout(timer);
  }, [undo]);

  const remove = (list: SavedTerm[]) => {
    removeTerms(list.map((t) => t.id));
    setUndo(list);
  };
  const save = (format: "md" | "csv") => {
    setExportsOpen(false);
    const list = shown.length ? shown : terms;
    if (format === "md") downloadFile(termsMarkdown(list), "术语本.md", "text/markdown");
    else downloadFile(termsCsv(list), "术语本.csv", "text/csv;charset=utf-8");
  };
  const reachable = (t: SavedTerm) =>
    papers.some(
      (p) =>
        p.id === routePaperId(t.paperId) &&
        (!t.paperId.startsWith("local:") || p.title === t.paperTitle),
    );
  const open = (t: SavedTerm) => {
    const toText = t.origin === "selection" && (t.itemId || t.blockId);
    jumpLater(toText ? "text" : "glossary", toText ? (t.itemId || t.blockId)! : t.term);
    go({
      page: "read",
      paperId: routePaperId(t.paperId),
      mode: toText ? "text" : "glossary",
    });
  };
  const chatAbout = (t: SavedTerm) => {
    const id = routePaperId(t.paperId);
    startWith([
      {
        kind: "term",
        term_id: t.id,
        term: t.term,
        explanation: t.explanation,
        note: t.note || "",
        ...(reachable(t) ? { paper_id: id } : {}),
        paper_title: t.paperTitle,
      },
      ...(reachable(t)
        ? [{ kind: "paper" as const, paper_id: id, title: t.paperTitle }]
        : []),
    ]);
    go({ page: "chat" });
  };

  return (
    <div className="page terms-page">
      <header className="page-head">
        <div>
          <p className="page-eyebrow">个人知识库</p>
          <h1>术语本</h1>
          <p>
            {terms.length
              ? `${terms.length} 个术语 · 来自 ${sources.length} 篇论文${todo ? ` · ${todo} 个待解释` : ""}`
              : "收藏与复习术语，仅存当前浏览器。"}
          </p>
        </div>
        {terms.length > 0 && (
          <div className="export" ref={exportRef}>
            <button
              className="button"
              aria-expanded={exportsOpen}
              onClick={() => setExportsOpen(!exportsOpen)}
            >
              <Icon name="download" size={15} />
              导出
            </button>
            {exportsOpen && (
              <div className="export-menu popover">
                <button onClick={() => save("md")}>
                  <span>复习笔记</span>
                  <small>Markdown</small>
                </button>
                <button onClick={() => save("csv")}>
                  <span>术语表格</span>
                  <small>CSV</small>
                </button>
                <p className="export-note">
                  {shown.length < terms.length && shown.length
                    ? `导出当前筛选出的 ${shown.length} 个术语。`
                    : `导出全部 ${terms.length} 个术语。`}
                </p>
              </div>
            )}
          </div>
        )}
      </header>

      {!terms.length ? (
        <div className="empty terms-empty">
          <Icon name="bookmark" size={24} />
          <strong>暂无术语</strong>
          <p>阅读时选中词语，或在论文的“术语”页点星标，即可收藏。</p>
          <a className="button" href={href({ page: "library" })}>
            <Icon name="library" size={15} />
            去文献库
          </a>
        </div>
      ) : (
        <>
          <div className="terms-toolbar">
            <label className="search-field">
              <Icon name="search" size={15} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索术语、解释或笔记"
                aria-label="搜索术语库"
              />
              {query && (
                <button aria-label="清除搜索" onClick={() => setQuery("")}>
                  <Icon name="close" size={13} />
                </button>
              )}
            </label>
            <div className="segmented" role="group" aria-label="按状态筛选">
              {(
                [
                  ["all", "全部", terms.length],
                  ["todo", "待解释", todo],
                  ["noted", "有笔记", noted],
                ] as const
              ).map(([value, label, count]) => (
                <button
                  key={value}
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {label} <small>{count}</small>
                </button>
              ))}
            </div>
            <label className="select">
              <span>排序</span>
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value as "recent" | "name")}
              >
                <option value="recent">最近加入</option>
                <option value="name">按名称</option>
              </select>
            </label>
          </div>
          {sources.length > 1 && (
            <div className="chip-row" role="group" aria-label="按论文筛选">
              <button aria-pressed={!activePaper} onClick={() => setPaper("")}>
                全部论文 <small>{terms.length}</small>
              </button>
              {sources.map(([id, s]) => (
                <button
                  key={id}
                  aria-pressed={activePaper === id}
                  title={s.title}
                  onClick={() => setPaper(id)}
                >
                  <span>{s.title}</span> <small>{s.count}</small>
                </button>
              ))}
            </div>
          )}
          <div className="saved-terms">
            {shown.map((t) => (
              <TermCard
                key={t.id}
                term={t}
                reachable={reachable(t)}
                chat={chat}
                onOpen={() => open(t)}
                onChat={() => chatAbout(t)}
                onRemove={() => remove([t])}
              />
            ))}
            {!shown.length && (
              <p className="empty-line">
                {q ? `没有匹配“${query}”的术语。` : "当前筛选下没有术语。"}
              </p>
            )}
          </div>
        </>
      )}

      {undo && (
        <div className="undo-bar" role="status">
          <span>
            已移除{undo.length === 1 ? `“${undo[0].term}”` : ` ${undo.length} 个术语`}
          </span>
          <button
            className="text-button"
            onClick={() => {
              restoreTerms(undo);
              setUndo(null);
            }}
          >
            撤销
          </button>
        </div>
      )}
    </div>
  );
}

/** A textarea that saves with Ctrl Enter or on blur and cancels with Esc. */
function TermEditor({
  value,
  label,
  placeholder,
  onSave,
  onCancel,
}: {
  value: string;
  label: string;
  placeholder: string;
  onSave: (text: string) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(value);
  const done = useRef(false);
  const finish = (save: boolean) => {
    if (done.current) return;
    done.current = true;
    if (save) onSave(draft.trim());
    else onCancel();
  };
  return (
    <textarea
      className="note-editor"
      autoFocus
      value={draft}
      rows={3}
      placeholder={`${placeholder}（支持 Markdown 与 $TeX$，Ctrl Enter 保存，Esc 取消）`}
      aria-label={label}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          finish(false);
        } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) finish(true);
      }}
    />
  );
}

function TermCard({
  term: t,
  reachable,
  chat,
  onOpen,
  onChat,
  onRemove,
}: {
  term: SavedTerm;
  reachable: boolean;
  chat: TermChat;
  onOpen: () => void;
  onChat: () => void;
  onRemove: () => void;
}) {
  const [editing, setEditing] = useState<"note" | "explanation" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const asked = useRef(0);
  const explained = Boolean(t.explanation.trim());
  const local = t.paperId.startsWith("local:");
  const aiReady = Boolean(chat) && reachable && !local;
  const aiWhy = !chat
    ? "当前未连接支持 AI 的本地服务"
    : !reachable || local
      ? "这篇论文不在当前文献库中，AI 无法读取原文"
      : "";
  const label = explanationLabel(t);

  const store = (patch: Parameters<typeof updateTerm>[1]) => {
    try {
      updateTerm(t.id, patch);
      setError("");
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    }
  };
  const explain = async () => {
    if (!aiReady || busy) return;
    const turn = ++asked.current;
    setBusy(true);
    setError("");
    const paper = routePaperId(t.paperId);
    const question = explainQuestion(t);
    try {
      const reply =
        chat === "papers"
          ? await askPapers({
              question,
              history: [],
              papers: [paper],
              passages: [],
              terms: [
                { term: t.term, explanation: "", note: t.note || "", paper_id: paper },
              ],
            })
          : await askPaper(paper, { question, history: [] });
      if (turn !== asked.current) return;
      const answer = reply.answer?.trim();
      if (!answer) throw new Error("模型没有返回解释，请稍后重试。");
      store({ explanation: answer, explanationBy: "ai", explanationModel: reply.model });
    } catch (e) {
      if (turn === asked.current)
        setError(`AI 解释失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      if (turn === asked.current) setBusy(false);
    }
  };
  const aiButton = (text: string) => (
    <button
      className="button small ai-button"
      disabled={!aiReady || busy}
      title={aiWhy || "结合论文原文生成解释"}
      onClick={() => void explain()}
    >
      <Icon name="sparkle" size={13} />
      {busy ? "正在解释…" : text}
    </button>
  );

  return (
    <article className={`saved-term ${explained ? "" : "is-todo"}`} aria-busy={busy}>
      <div className="saved-term-head">
        <h3>
          <AnalysisText text={t.term} />
        </h3>
        <span className="origin-tag">
          {t.origin === "glossary" ? "论文术语" : "划词收藏"}
        </span>
        <div className="saved-term-actions">
          <button
            className="icon-button small"
            title={t.note ? "编辑笔记" : "添加笔记"}
            aria-label={`${t.note ? "编辑" : "添加"}“${t.term}”的笔记`}
            onClick={() => setEditing("note")}
          >
            <Icon name="pencil" size={14} />
          </button>
          {chat && (
            <button
              className="icon-button small"
              title="带着这个术语去对话"
              aria-label={`就“${t.term}”提问`}
              onClick={onChat}
            >
              <Icon name="chat" size={14} />
            </button>
          )}
          <button
            className="icon-button small"
            title="移出术语本"
            aria-label={`移出“${t.term}”`}
            onClick={onRemove}
          >
            <Icon name="trash" size={14} />
          </button>
        </div>
      </div>

      {editing === "explanation" ? (
        <TermEditor
          value={t.explanation}
          label="解释"
          placeholder="用自己的话写下定义…"
          onCancel={() => setEditing(null)}
          onSave={(text) => {
            if (
              text === t.explanation.trim() ||
              store({
                explanation: text,
                explanationBy: "me",
                explanationModel: undefined,
              })
            )
              setEditing(null);
          }}
        />
      ) : explained ? (
        <div className="saved-term-text">
          <Markdown text={t.explanation} />
          <div className="saved-term-meta">
            {label && (
              <span className={`by-tag by-${t.explanationBy || "paper"}`}>
                {t.explanationBy === "ai" && <Icon name="sparkle" size={11} />}
                {label}
                {t.explanationModel ? ` · ${t.explanationModel}` : ""}
              </span>
            )}
            <button className="text-button" onClick={() => setEditing("explanation")}>
              改写
            </button>
            {t.explanationBy === "ai" && aiReady && (
              <button
                className="text-button"
                disabled={busy}
                onClick={() => void explain()}
              >
                {busy ? "正在解释…" : "重新生成"}
              </button>
            )}
          </div>
        </div>
      ) : (
        <div className="saved-term-pending">
          {!t.note && <p>还没有解释。</p>}
          <div className="saved-term-pending-actions">
            {aiButton(t.note ? "AI 补充解释" : "AI 解释")}
            <button className="button small" onClick={() => setEditing("explanation")}>
              <Icon name="pencil" size={13} />
              自己写
            </button>
          </div>
          {aiWhy && <small className="hint">{aiWhy}</small>}
        </div>
      )}

      {editing === "note" ? (
        <TermEditor
          value={t.note || ""}
          label="笔记"
          placeholder="自己的理解、例子或联想…"
          onCancel={() => setEditing(null)}
          onSave={(text) => {
            if (text === (t.note || "") || store({ note: text })) setEditing(null);
          }}
        />
      ) : (
        t.note && (
          <div
            className="saved-term-note"
            title="双击编辑"
            onDoubleClick={() => setEditing("note")}
          >
            <span className="saved-term-note-label">我的笔记</span>
            <Markdown text={t.note} />
          </div>
        )
      )}
      {error && (
        <p className="saved-term-error" role="alert">
          {error}
        </p>
      )}
      {t.quote && (
        <blockquote className="saved-term-quote">
          <TexText text={t.quote} />
          {t.page ? <cite>P{t.page}</cite> : null}
        </blockquote>
      )}
      <footer className="saved-term-foot">
        {reachable ? (
          <button className="source-link" onClick={onOpen} title="回到论文中的出处">
            <Icon name="file" size={13} />
            <span>{t.paperTitle}</span>
            <Icon name="arrow" size={12} />
          </button>
        ) : (
          <span className="source-link is-off" title="这篇论文不在当前文献库中">
            <Icon name="file" size={13} />
            <span>{t.paperTitle}</span>
          </span>
        )}
        <time dateTime={t.added}>{day(t.added)}</time>
      </footer>
    </article>
  );
}
