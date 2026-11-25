import { useEffect, useRef, useState } from "react";
import { Icon } from "./icons";
import { go } from "./router";
import {
  addRef,
  blankConversation,
  discard,
  ensureSession,
  getSession,
  promote,
  retry,
  send,
  setRefs,
  stop,
  storedLocally,
  useSession,
  type ChatMessage,
  type ChatRef,
} from "./chat";
import { Composer, Suggestions, Thread } from "./ChatThread";
import type { PaperSummary } from "./api";

export const quickKey = (paperId: string) => `quick:${paperId}`;

function ensureQuick(paperId: string, title: string, notify = true) {
  return ensureSession(
    quickKey(paperId),
    () => blankConversation([{ kind: "paper", paper_id: paperId, title }]),
    false,
    notify,
  );
}

/** Adds a reference (e.g. a picked passage) to this paper's quick chat. */
export function addToQuick(paperId: string, title: string, ref: ChatRef): string {
  const key = quickKey(paperId);
  ensureQuick(paperId, title);
  const added = addRef(getSession(key)!.doc.refs, ref);
  setRefs(key, added.refs);
  return added.error || "";
}

/**
 * 问一问: a side conversation beside the paper, like a scratch chat. It
 * keeps an automatic browser draft until it is saved to the 对话 page.
 */
export function QuickChat({
  paperId,
  title,
  papers,
  canHighlights,
  onClose,
  onToast,
  onPin,
}: {
  paperId: string;
  title: string;
  papers: PaperSummary[];
  canHighlights: boolean;
  onClose: () => void;
  onToast: (message: string) => void;
  onPin?: (
    answer: ChatMessage,
    question: ChatMessage | undefined,
  ) => Promise<void> | void;
}) {
  const key = quickKey(paperId);
  ensureQuick(paperId, title, false);
  const session = useSession(key);
  const [saving, setSaving] = useState(false);
  const panel = useRef<HTMLElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Esc first closes whatever sits on top (a selection toolbar, a picker, a modal).
      if (
        e.key !== "Escape" ||
        e.defaultPrevented ||
        document.querySelector(".modal-backdrop")
      )
        return;
      onClose();
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!session) return null;
  const { doc, pending } = session;
  const keep = async () => {
    setSaving(true);
    try {
      const id = await promote(key);
      onToast(storedLocally() ? "已保存到对话（仅本浏览器）" : "已保存到对话");
      go({ page: "chat", id });
    } catch (e) {
      onToast(`未能保存：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <aside className="quick-chat" ref={panel} aria-label="问一问">
      <header className="quick-head">
        <span className="quick-mark">
          <Icon name="chat" size={15} />
        </span>
        <div>
          <strong>问一问</strong>
          <small>草稿自动保存在本浏览器</small>
        </div>
        <button
          className="icon-button small"
          title="保存对话"
          aria-label="保存对话"
          disabled={!doc.messages.length || Boolean(pending) || saving}
          onClick={keep}
        >
          <Icon name="bookmark" size={15} />
        </button>
        <button
          className="icon-button small"
          title="清空，重新开始"
          aria-label="清空对话"
          disabled={!doc.messages.length && doc.refs.length <= 1}
          onClick={() => {
            discard(key);
            ensureQuick(paperId, title);
          }}
        >
          <Icon name="trash" size={15} />
        </button>
        <button
          className="icon-button small"
          title="关闭（Esc）"
          aria-label="关闭问一问"
          onClick={onClose}
        >
          <Icon name="close" size={15} />
        </button>
      </header>
      <Thread
        session={session}
        papers={papers}
        onRetry={(id) => retry(key, id)}
        onStop={() => stop(key)}
        onPin={onPin}
        empty={
          <div className="quick-empty">
            <p>问点什么，一起读懂这篇论文。</p>
            <p className="muted">拖入选文，或输入 @ 添加引用。</p>
            <Suggestions refs={doc.refs} onPick={(q) => send(key, q)} />
          </div>
        }
      />
      <Composer
        draftKey={key}
        refs={doc.refs}
        onRefs={(refs) => setRefs(key, refs)}
        papers={papers}
        locked={paperId}
        pending={Boolean(pending)}
        canHighlights={canHighlights}
        onSend={(text) => send(key, text)}
        onStop={() => stop(key)}
        autoFocus
        placeholder="关于这篇论文，想问什么？"
      />
    </aside>
  );
}
