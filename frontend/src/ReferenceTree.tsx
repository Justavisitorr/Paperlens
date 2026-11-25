import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "./dialog";
import {
  loadReferenceBranch,
  mergeReferenceBranches,
  referenceBranchStatus,
  type ReferenceTreeBranch,
  type ReferenceTreeNode,
} from "./referenceTreeApi";
import "./styles/reference-tree.css";

interface BranchState {
  data?: ReferenceTreeBranch;
  loading: boolean;
  error?: string;
}

export function ReferenceTree({
  paperId,
  referenceId,
  label,
}: {
  paperId: string;
  referenceId: string;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="text-button"
        onClick={() => setOpen(true)}
        title="沿这篇文献的参考文献继续查找"
      >
        引用树
      </button>
      {open && (
        <ReferenceTreeDialog
          paperId={paperId}
          referenceId={referenceId}
          label={label}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

export function ReferenceTreeDialog({
  paperId,
  referenceId,
  label,
  onClose,
}: {
  paperId: string;
  referenceId: string;
  label: string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const controllers = useRef(new Map<string, AbortController>());
  const mounted = useRef(true);
  const [branches, setBranches] = useState<Record<string, BranchState>>({});
  const [root, setRoot] = useState("");
  useFocusTrap(dialog);
  const load = useCallback(
    async (node = "", offset = 0, refresh = false) => {
      if (controllers.current.has(node)) return;
      const controller = new AbortController();
      controllers.current.set(node, controller);
      setBranches((old) => ({
        ...old,
        [node]: { ...old[node], loading: true, error: "" },
      }));
      try {
        const next = await loadReferenceBranch(paperId, referenceId, {
          node,
          offset,
          refresh,
          signal: controller.signal,
        });
        if (mounted.current && !controller.signal.aborted)
          setBranches((old) => ({
            ...old,
            [node]:
              next.state !== "available" && offset > 0
                ? { ...old[node], loading: false, error: next.message }
                : { data: mergeReferenceBranches(old[node]?.data, next), loading: false },
          }));
      } catch (error) {
        if (mounted.current && !controller.signal.aborted)
          setBranches((old) => ({
            ...old,
            [node]: {
              ...old[node],
              loading: false,
              error:
                error instanceof Error ? error.message : "引用树暂时无法加载，请重试。",
            },
          }));
      } finally {
        if (controllers.current.get(node) === controller)
          controllers.current.delete(node);
      }
    },
    [paperId, referenceId],
  );
  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
      controllers.current.forEach((controller) => controller.abort());
      controllers.current.clear();
    };
  }, [load]);
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", escape, true);
    return () => {
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", escape, true);
    };
  }, [onClose]);
  const repeats = useMemo(() => {
    const counts = new Map<string, number>();
    Object.values(branches).forEach(({ data }) =>
      data?.references.forEach((node) =>
        counts.set(node.id, (counts.get(node.id) || 0) + 1),
      ),
    );
    return counts;
  }, [branches]);
  const current = branches[root];
  const rebase = (node: string) => {
    setRoot(node);
    if (!branches[node]) void load(node);
  };
  const content = (
    <div
      className="modal-backdrop reference-tree-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="reference-tree-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="多级参考文献树"
        tabIndex={-1}
        ref={dialog}
      >
        <header className="reference-tree-heading">
          <div>
            <h2>引用树</h2>
            <p>文献 → 它引用的文献</p>
          </div>
          <button type="button" aria-label="关闭引用树" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="reference-tree-scroll">
          <p className="reference-tree-intro">
            按需展开，每次最多加载 20 篇。关系来自
            OpenAlex；未收录的引用不会显示，原文仍是核对依据。
          </p>
          {root && (
            <button type="button" className="text-button" onClick={() => setRoot("")}>
              ← 返回最初引用
            </button>
          )}
          <div className="reference-tree-root">
            <strong>{current?.data?.node?.title || label}</strong>
            {current?.data?.node?.year && <span> · {current.data.node.year}</span>}
          </div>
          <BranchContents
            state={current}
            node={root}
            ancestors={current?.data?.node ? [current.data.node.id] : []}
            branches={branches}
            repeats={repeats}
            load={load}
            rebase={rebase}
          />
        </div>
      </div>
    </div>
  );
  return typeof document === "undefined" ? content : createPortal(content, document.body);
}

interface TreeContext {
  branches: Record<string, BranchState>;
  repeats: Map<string, number>;
  load: (node?: string, offset?: number, refresh?: boolean) => Promise<void>;
  rebase: (node: string) => void;
}

function BranchContents({
  state,
  node,
  ancestors,
  ...context
}: TreeContext & {
  state?: BranchState;
  node: string;
  ancestors: string[];
}) {
  const data = state?.data;
  const problem = state?.error || (data && data.state !== "available" && data.message);
  const retryable =
    !!state?.error ||
    !data ||
    ["error", "rate_limited", "not_found", "unavailable"].includes(data.state);
  return (
    <div className="reference-tree-branch">
      {data?.source_url && (
        <p className="reference-tree-source">
          <a href={data.source_url} target="_blank" rel="noopener noreferrer">
            {data.source} 记录 ↗
          </a>
          {data.total !== null && <span>收录 {data.total} 条引用</span>}
          {data.cached && <span>已缓存</span>}
        </p>
      )}
      {problem && (
        <div className="reference-tree-message" role="status">
          <span>{problem}</span>
          {retryable && !state?.loading && (
            <button
              type="button"
              className="text-button"
              onClick={() =>
                void context.load(node, state?.error ? data?.next_offset || 0 : 0, true)
              }
            >
              重试
            </button>
          )}
        </div>
      )}
      {!!data?.references.length && (
        <ul className="reference-tree-list">
          {data.references.map((entry) => (
            <TreeEntry key={entry.id} entry={entry} ancestors={ancestors} {...context} />
          ))}
        </ul>
      )}
      {state?.loading && (
        <p className="reference-tree-message" role="status">
          正在读取参考文献…
        </p>
      )}
      {!state?.loading &&
        data?.next_offset !== null &&
        data?.next_offset !== undefined && (
          <button
            type="button"
            className="text-button reference-tree-more"
            onClick={() => void context.load(node, data.next_offset!)}
          >
            加载更多（已显示 {data.references.length} / {data.total}）
          </button>
        )}
    </div>
  );
}

function TreeEntry({
  entry,
  ancestors,
  ...context
}: TreeContext & { entry: ReferenceTreeNode; ancestors: string[] }) {
  const [expanded, setExpanded] = useState(false);
  const state = context.branches[entry.id];
  const status = referenceBranchStatus(entry.id, ancestors);
  const repeated = (context.repeats.get(entry.id) || 0) > 1;
  return (
    <li className="reference-tree-entry">
      <div className="reference-tree-card">
        <a
          className="reference-tree-title"
          href={entry.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {entry.title} ↗
        </a>
        <div className="reference-tree-meta">
          {entry.year && <span>{entry.year}</span>}
          {repeated && status !== "cycle" && <span>也见其他分支</span>}
          {status === "cycle" ? (
            <span>已在上级路径出现，停止循环</span>
          ) : status === "limit" ? (
            <button
              type="button"
              className="text-button"
              onClick={() => context.rebase(entry.id)}
            >
              从此文献继续 →
            </button>
          ) : (
            <button
              type="button"
              className="text-button"
              aria-expanded={expanded}
              aria-label={`${expanded ? "收起" : "展开"}引用：${entry.title}`}
              onClick={() => {
                setExpanded((old) => !old);
                if (!expanded && !state) void context.load(entry.id);
              }}
            >
              {expanded ? "收起引用" : "展开引用"} {expanded ? "−" : "+"}
            </button>
          )}
        </div>
      </div>
      {expanded && status === "expandable" && (
        <BranchContents
          state={state}
          node={entry.id}
          ancestors={[...ancestors, entry.id]}
          {...context}
        />
      )}
    </li>
  );
}
