import { useEffect, useRef, useState } from "react";
import { confirmInputBudget } from "./inputBudget";
import {
  generateAutoNote,
  planAutoNotes,
  type AutoNoteContext,
  type AutoNotePlan,
  type AutoNoteType,
} from "./api";
import { openSettings, settingsHref } from "./settingsNavigation";
import "./styles/23-reading-notes.css";

export const AUTO_NOTE_CONTEXT_LABELS: Record<AutoNoteContext, string> = {
  nearby: "前后段落",
  section: "所在小节",
  paper: "全文相关",
};
export const AUTO_NOTE_TYPE_LABELS: Record<AutoNoteType, string> = {
  summary: "要点摘要",
  explain: "解释概念",
  connections: "前后联系",
  questions: "阅读问题",
  check: "疑点核对",
};
/** Each successful paragraph is saved by the server before we request the next. */
export function AutoNotes({
  paperId,
  anchorId,
  onSaved,
  onClose,
}: {
  paperId: string;
  anchorId?: string;
  onSaved: () => Promise<unknown>;
  onClose: () => void;
}) {
  const [scope, setScope] = useState<"section" | "paper">(anchorId ? "section" : "paper");
  const [plan, setPlan] = useState<AutoNotePlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [progress, setProgress] = useState({ done: 0, generated: 0 });
  const [errors, setErrors] = useState<string[]>([]);
  const [finished, setFinished] = useState(false);
  const stop = useRef(false);
  const running = useRef(false);
  useEffect(
    () => () => {
      stop.current = true;
    },
    [],
  );
  useEffect(() => {
    let live = true;
    setLoading(true);
    setPlan(null);
    setErrors([]);
    setFinished(false);
    void planAutoNotes(paperId, { scope, anchor_id: anchorId })
      .then((value) => {
        if (live) setPlan(value);
      })
      .catch((error) => {
        if (live) setErrors([String(error.message || error)]);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [paperId, scope, anchorId]);
  const generate = async () => {
    if (!plan || running.current || !plan.planned) return;
    running.current = true;
    stop.current = false;
    setBusy(true);
    setStopping(false);
    setFinished(false);
    setErrors([]);
    setProgress({ done: 0, generated: 0 });
    try {
      // The preview's request limit also caps this entire browser batch.
      for (const item of plan.items.slice(0, plan.max_calls)) {
        if (stop.current) break;
        const request = {
          scope,
          anchor_id: anchorId,
          item_id: item.item_id,
          source_version: plan.source_version,
        };
        let result = await generateAutoNote(paperId, request);
        const pending = result.errors.find((error) => error.input_budget)?.input_budget;
        if (pending && !stop.current) {
          const approval = confirmInputBudget(pending, "生成笔记");
          if (approval)
            result = await generateAutoNote(paperId, {
              ...request,
              input_budget: approval,
            });
        }
        setProgress((old) => ({
          done: old.done + 1,
          generated: old.generated + result.generated,
        }));
        if (result.errors.length) {
          setErrors((old) => [...old, ...result.errors.map((error) => error.message)]);
          // Do not spend the rest of the batch after a failed model request.
          break;
        }
        await onSaved();
      }
    } catch (error) {
      setErrors((old) => [
        ...old,
        error instanceof Error ? error.message : String(error),
      ]);
    } finally {
      try {
        await onSaved();
      } finally {
        running.current = false;
        setBusy(false);
        setFinished(true);
      }
    }
  };
  return (
    <div className="note-editor-backdrop">
      <section
        className="note-composer auto-note-composer"
        role="dialog"
        aria-modal="true"
        aria-label="生成 自动笔记"
      >
        <header className="reading-note-head">
          <strong>生成笔记</strong>
          <button
            className="icon-button small"
            aria-label="关闭自动笔记"
            disabled={busy}
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <div className="note-kind-switch" role="group" aria-label="笔记生成范围">
          <button
            className="button small"
            aria-pressed={scope === "section"}
            disabled={!anchorId || busy}
            onClick={() => setScope("section")}
          >
            当前小节
          </button>
          <button
            className="button small"
            aria-pressed={scope === "paper"}
            disabled={busy}
            onClick={() => setScope("paper")}
          >
            整篇论文
          </button>
        </div>
        {loading && <p role="status">正在准备段落…</p>}
        {plan && (
          <>
            <p>
              {plan.types.map((type) => AUTO_NOTE_TYPE_LABELS[type]).join(" · ") ||
                "请先在设置中选择笔记类型"}
            </p>
            <dl className="auto-note-plan">
              <div>
                <dt>本次选取</dt>
                <dd>
                  {Math.min(plan.planned, plan.max_calls)} 处 / 共 {plan.eligible} 处
                </dd>
              </div>
              <div>
                <dt>参考范围</dt>
                <dd>{AUTO_NOTE_CONTEXT_LABELS[plan.context_scope]}</dd>
              </div>
            </dl>
            <p className="note-composer-hint">分散选取内容，跳过已有笔记。</p>
            {!busy && !finished && (
              <p>
                {plan.planned
                  ? `将调用模型，最多 ${plan.max_calls} 次；费用取决于所选模型和参考内容。`
                  : "本次没有需要生成的内容。"}
              </p>
            )}
          </>
        )}
        {(busy || finished) && (
          <p role="status">
            {busy
              ? stopping
                ? "本段完成后停止"
                : "正在写笔记"
              : stopping
                ? "已停止"
                : "本次完成"}
            {` · 已保存 ${progress.generated} 条`}
            {busy && plan
              ? ` · ${progress.done}/${Math.min(plan.planned, plan.max_calls)}`
              : ""}
          </p>
        )}
        {errors.length > 0 && (
          <p role="alert" className="error-line">
            {[...new Set(errors)].join(" ")}
          </p>
        )}
        <footer className="reading-note-actions">
          {!busy && (
            <a
              className="button small settings-shortcut"
              href={settingsHref("autoNotes")}
              onClick={(event) => {
                openSettings("autoNotes", event);
                onClose();
              }}
            >
              配置自动笔记
            </a>
          )}
          {busy ? (
            <button
              className="button small"
              disabled={stopping}
              onClick={() => {
                stop.current = true;
                setStopping(true);
              }}
            >
              停止生成
            </button>
          ) : finished ? (
            <button className="button small primary" onClick={onClose}>
              回到阅读
            </button>
          ) : (
            <button
              className="button small primary"
              disabled={loading || !plan?.max_calls || !plan.types.length}
              onClick={() => void generate()}
            >
              生成笔记
            </button>
          )}
        </footer>
      </section>
    </div>
  );
}
