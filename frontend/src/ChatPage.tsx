import { useEffect, useMemo, useState } from "react";
import { Icon } from "./icons";
import { go, href } from "./router";
import { downloadFile, fileStem } from "./export";
import { useDeletedConversations } from "./conversationUndo";
import "./styles/31-interaction-recovery.css";
import {
  addRef,
  blankConversation,
  conversationMarkdown,
  ensureSession,
  newId,
  openConversation,
  refreshList,
  removeConversation,
  restoreConversation,
  retry,
  send,
  setPinned,
  setRefs,
  stop,
  storedLocally,
  takeSeed,
  updateDoc,
  useConversationList,
  useSession,
  type ChatRef,
} from "./chat";
import { Composer, Suggestions, Thread } from "./ChatThread";
import type { ConversationSummary, PaperSummary } from "./api";

const DAY = 86400000;
function dayGroup(iso: string, now = new Date()) {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = new Date(iso).getTime();
  if (t >= start) return "今天";
  if (t >= start - DAY) return "昨天";
  if (t >= start - 6 * DAY) return "近 7 天";
  if (t >= start - 29 * DAY) return "近 30 天";
  return "更早";
}
const when = (iso: string) => {
  const d = new Date(iso);
  return Date.now() - d.getTime() < DAY
    ? d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
};

/**
 * 对话: conversations across papers. Reference several papers, passages and
 * saved terms; every answer points back to the source sentences it rests on.
 */
export function ChatPage({
  id,
  papers,
  available,
  canHighlights,
  onToast,
}: {
  id?: string;
  papers: PaperSummary[];
  /** Why chat is unavailable, if it is; null while the service is still connecting. */
  available: true | string | null;
  canHighlights: boolean;
  onToast: (message: string) => void;
}) {
  const list = useConversationList();
  const [query, setQuery] = useState("");
  const deleted = useDeletedConversations();
  const [restoring, setRestoring] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState<Set<string>>(new Set());
  // A fresh conversation gets its id up front; it is saved with the first question.
  const [draft, setDraft] = useState(newId);
  const key = id || draft;
  const [loading, setLoading] = useState<string>("");
  /** Small screens show one pane: a fresh conversation counts as open. */
  const [composing, setComposing] = useState(false);

  useEffect(() => {
    if (available === true) void refreshList();
  }, [available]);
  const ready = available === true;
  useEffect(() => {
    if (id || !ready) return;
    const seed = takeSeed();
    const fresh = newId();
    ensureSession(fresh, () => ({ ...blankConversation(seed || []), id: fresh }), true);
    setDraft(fresh);
    if (seed?.length) setComposing(true);
  }, [id, ready]);
  useEffect(() => {
    if (!id || !ready) return;
    let alive = true;
    setLoading("");
    openConversation(id).then(
      () => alive && setLoading(""),
      (e) => alive && setLoading(e instanceof Error ? e.message : String(e)),
    );
    return () => {
      alive = false;
    };
  }, [id, ready]);
  const session = useSession(key);
  // The first question saves a draft; give it its own address.
  useEffect(() => {
    if (!id && session?.doc.messages.length)
      go({ page: "chat", id: session.doc.id }, true);
  }, [id, session?.doc.messages.length, session?.doc.id]);

  const groups = useMemo(() => {
    const q = query.trim().toLocaleLowerCase();
    const items = (list.items || []).filter(
      (c) => !q || `${c.title} ${c.preview}`.toLocaleLowerCase().includes(q),
    );
    const out: [string, ConversationSummary[]][] = [];
    for (const c of items) {
      const label = c.pinned ? "置顶" : dayGroup(c.updated_at);
      const group = out.find(([l]) => l === label);
      if (group) group[1].push(c);
      else out.push([label, [c]]);
    }
    return out.sort(([a], [b]) => (a === "置顶" ? -1 : b === "置顶" ? 1 : 0));
  }, [list.items, query]);

  if (available === null)
    return (
      <div className="page">
        <div className="loading-block" aria-busy="true">
          <span className="spinner" />
          正在连接本地服务…
        </div>
      </div>
    );
  if (available !== true)
    return (
      <div className="page">
        <div className="empty">
          <Icon name="chat" size={22} />
          <strong>对话需要本地服务</strong>
          <p>{available}</p>
          <a className="button" href={href({ page: "library" })}>
            返回论文列表
          </a>
        </div>
      </div>
    );

  const remove = async (target: string) => {
    if (deleting.has(target)) return;
    setDeleting((old) => new Set(old).add(target));
    try {
      await removeConversation(target);
      if (target === id) go({ page: "chat" });
    } catch (e) {
      onToast(`删除未完成：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setDeleting((old) => new Set([...old].filter((value) => value !== target)));
    }
  };
  const doc = session?.doc;
  const exportMd = () =>
    doc &&
    downloadFile(
      conversationMarkdown(doc),
      `${fileStem(doc.title || "对话")}.md`,
      "text/markdown;charset=utf-8",
    );

  return (
    <div className={`chat-page ${id || composing ? "has-thread" : ""}`}>
      <aside className="chat-list" aria-label="对话列表">
        <div className="chat-list-head">
          <h1>对话</h1>
          <a
            className="button small primary"
            href={href({ page: "chat" })}
            title="新对话"
            onClick={() => setComposing(true)}
          >
            <Icon name="plus" size={14} />
            新对话
          </a>
        </div>
        <label className="search-field">
          <Icon name="search" size={14} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索对话"
            aria-label="搜索对话"
          />
        </label>
        {deleted.length > 0 && (
          <details className="chat-deleted" open>
            <summary>最近删除 · {deleted.length}</summary>
            <p className="footnote">离开本页仍可撤销，请在关闭标签页前恢复。</p>
            {deleted.map((doc) => (
              <div className="undo-bar" key={doc.id}>
                <span>{doc.title || "未命名对话"}</span>
                <button
                  className="text-button"
                  disabled={restoring.has(doc.id)}
                  aria-label={`恢复对话 ${doc.title || "未命名对话"}`}
                  onClick={async () => {
                    setRestoring((old) => new Set(old).add(doc.id));
                    try {
                      await restoreConversation(doc);
                    } catch (error) {
                      onToast(`恢复未完成，可重试：${String(error)}`);
                    } finally {
                      setRestoring(
                        (old) => new Set([...old].filter((id) => id !== doc.id)),
                      );
                    }
                  }}
                >
                  {restoring.has(doc.id) ? "恢复中" : "撤销删除"}
                </button>
              </div>
            ))}
          </details>
        )}
        <nav className="chat-rows">
          {list.items === null && !list.error && <div className="skeleton" />}
          {list.error && <p className="error-line">{list.error}</p>}
          {groups.map(([label, items]) => (
            <section key={label}>
              <div className="side-label">{label}</div>
              {items.map((c) => (
                <div key={c.id} className={`chat-row ${c.id === id ? "is-active" : ""}`}>
                  <a
                    href={href({ page: "chat", id: c.id })}
                    aria-current={c.id === id ? "page" : undefined}
                  >
                    <strong>{c.title || "未命名对话"}</strong>
                    <small>
                      <span>{c.preview}</span>
                    </small>
                    <span className="chat-row-meta">
                      {c.paper_ids.length > 0 && (
                        <span title={`引用 ${c.paper_ids.length} 篇论文`}>
                          <Icon name="file" size={12} />
                          {c.paper_ids.length}
                        </span>
                      )}
                      <time>{when(c.updated_at)}</time>
                    </span>
                  </a>
                  <div className="chat-row-actions">
                    <button
                      className="icon-button small"
                      aria-label={c.pinned ? "取消置顶" : "置顶"}
                      title={c.pinned ? "取消置顶" : "置顶"}
                      aria-pressed={c.pinned}
                      onClick={() => void setPinned(c.id, !c.pinned)}
                    >
                      <Icon name="pin" size={14} />
                    </button>
                    <button
                      className="icon-button small"
                      aria-label={`删除对话 ${c.title}`}
                      title="删除"
                      disabled={deleting.has(c.id)}
                      onClick={() => void remove(c.id)}
                    >
                      <Icon name="trash" size={14} />
                    </button>
                  </div>
                </div>
              ))}
            </section>
          ))}
          {list.items?.length === 0 && (
            <p className="side-empty">还没有保存的对话。提出第一个问题后会出现在这里。</p>
          )}
          {!!list.items?.length && !groups.length && (
            <p className="side-empty">没有匹配“{query}”的对话。</p>
          )}
        </nav>
        <p className="chat-list-foot">
          <Icon name={storedLocally() ? "warning" : "shield"} size={13} />
          {storedLocally() ? "对话只保存在本浏览器" : "对话保存在本地服务的输出目录"}
        </p>
      </aside>

      <section className="chat-main" aria-label="对话内容">
        {loading ? (
          <div className="empty">
            <Icon name="warning" size={22} />
            <strong>这段对话暂时无法打开</strong>
            <p>{loading}</p>
          </div>
        ) : !session || !doc ? (
          <div className="loading-block" aria-busy="true">
            <span className="spinner" />
            正在打开对话…
          </div>
        ) : (
          <>
            <header className="chat-head">
              <a
                className="icon-button small chat-back"
                href={href({ page: "chat" })}
                aria-label="返回对话列表"
                onClick={() => setComposing(false)}
              >
                <Icon name="chevron" size={16} style={{ transform: "rotate(180deg)" }} />
              </a>
              <TitleField
                key={doc.id}
                value={doc.title}
                onChange={(title) => updateDoc(key, (d) => ({ ...d, title }))}
              />
              {session.saveError && (
                <span className="warn-line" title={session.saveError}>
                  <Icon name="warning" size={13} />
                  未能保存
                </span>
              )}
              {doc.messages.length > 0 && (
                <button className="button small" onClick={exportMd}>
                  <Icon name="download" size={14} />
                  导出
                </button>
              )}
            </header>
            <Thread
              session={session}
              papers={papers}
              onRetry={(m) => retry(key, m)}
              onStop={() => stop(key)}
              empty={
                <Welcome
                  refs={doc.refs}
                  papers={papers}
                  onRefs={(refs) => setRefs(key, refs)}
                  onAsk={(q) => send(key, q)}
                />
              }
            />
            <Composer
              draftKey={key}
              refs={doc.refs}
              onRefs={(refs) => setRefs(key, refs)}
              papers={papers}
              pending={Boolean(session.pending)}
              canHighlights={canHighlights}
              onSend={(text) => send(key, text)}
              onStop={() => stop(key)}
              autoFocus
              placeholder={doc.refs.length ? "继续提问…" : "先输入 @ 引用论文，再提问…"}
            />
          </>
        )}
      </section>
    </div>
  );
}

function TitleField({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const commit = () => {
    const next = text.trim().slice(0, 200);
    if (next !== value) onChange(next);
  };
  return (
    <input
      className="chat-title"
      value={text}
      placeholder="新对话"
      aria-label="对话标题"
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
        if (e.key === "Escape") {
          setText(value);
          e.currentTarget.blur();
        }
      }}
    />
  );
}

/** Before the first question: what this page does, and a quick way to pick papers. */
function Welcome({
  refs,
  papers,
  onRefs,
  onAsk,
}: {
  refs: ChatRef[];
  papers: PaperSummary[];
  onRefs: (refs: ChatRef[]) => void;
  onAsk: (question: string) => void;
}) {
  const chosen = new Set(refs.flatMap((r) => (r.kind === "paper" ? [r.paper_id] : [])));
  const recent = papers.filter((p) => p.status !== "failed").slice(0, 6);
  return (
    <div className="chat-welcome">
      <h2>聊聊论文</h2>
      <p>选好论文，开始提问。</p>
      {recent.length > 0 && (
        <div className="welcome-papers">
          <div className="side-label">选择论文</div>
          {recent.map((p) => {
            const on = chosen.has(p.id);
            return (
              <button
                key={p.id}
                className={`welcome-paper ${on ? "is-on" : ""}`}
                aria-pressed={on}
                onClick={() =>
                  onRefs(
                    on
                      ? refs.filter((r) => r.kind === "term" || r.paper_id !== p.id)
                      : addRef(refs, { kind: "paper", paper_id: p.id, title: p.title })
                          .refs,
                  )
                }
              >
                <Icon name={on ? "check" : "file"} size={14} />
                <span>{p.title}</span>
              </button>
            );
          })}
          {papers.length > recent.length && (
            <small className="muted">更多论文与术语：在输入框中输入 @</small>
          )}
        </div>
      )}
      {!papers.length && (
        <p className="muted">
          先<a href={href({ page: "upload" })}>导入论文</a>，再一起讨论。
        </p>
      )}
      <Suggestions refs={refs} onPick={onAsk} />
    </div>
  );
}
