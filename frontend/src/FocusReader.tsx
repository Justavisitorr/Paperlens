import { AnalysisText } from "./AnalysisText";
import { useEffect, useMemo, useRef, useState } from "react";
import { useFocusTrap } from "./dialog";
import { Icon } from "./icons";
import { NodeDetails } from "./EvidencePanel";
import { kindLabels } from "./graph";
import { isTyping } from "./Shortcuts";
import type { DashboardReport, GraphView, PaperNode } from "./types";

/** Depth-first order of a view: how the argument reads from top to bottom. */
function readingOrder(report: DashboardReport, graph: GraphView): PaperNode[] {
  const inView = new Set(graph.node_ids);
  const nodes = report.analysis.nodes.filter((n) => inView.has(n.id));
  const kids = new Map<string, PaperNode[]>();
  const roots: PaperNode[] = [];
  for (const n of nodes) {
    if (n.parent_id && inView.has(n.parent_id)) {
      kids.set(n.parent_id, [...(kids.get(n.parent_id) || []), n]);
    } else roots.push(n);
  }
  const order: PaperNode[] = [];
  const seen = new Set<string>();
  const walk = (n: PaperNode) => {
    if (seen.has(n.id)) return;
    seen.add(n.id);
    order.push(n);
    (kids.get(n.id) || []).forEach(walk);
  };
  roots.forEach(walk);
  return order;
}

/**
 * One argument at a time, full width, in reading order. The map stays where
 * it was; leaving brings the reader back to the node they ended on.
 */
export function FocusReader({
  report,
  graph,
  startId,
  onClose,
}: {
  report: DashboardReport;
  graph: GraphView;
  startId: string;
  onClose: (id: string) => void;
}) {
  const [id, setId] = useState(startId);
  const order = useMemo(() => readingOrder(report, graph), [report, graph]);
  const nodes = report.analysis.nodes;
  const node = nodes.find((n) => n.id === id) || order[0];
  const index = order.findIndex((n) => n.id === node?.id);
  const prev = index > 0 ? order[index - 1] : undefined;
  const next = index >= 0 ? order[index + 1] : undefined;
  const children = nodes.filter((n) => n.parent_id === node?.id);
  const trail: PaperNode[] = [];
  for (let up = node?.parent_id; up && trail.length < 12;) {
    const parent = nodes.find((n) => n.id === up);
    if (!parent) break;
    trail.unshift(parent);
    up = parent.parent_id;
  }
  const scroller = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const latest = useRef({ node, prev, next, trail, onClose });
  latest.current = { node, prev, next, trail, onClose };

  useFocusTrap(dialog);
  useEffect(() => {
    // Capture phase, so the map underneath never sees these keys.
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e)) return;
      if (document.querySelector(".modal-backdrop")) return;
      const { node, prev, next, trail, onClose } = latest.current;
      const go = (n?: PaperNode) => n && setId(n.id);
      const handled: Record<string, () => void> = {
        Escape: () => node && onClose(node.id),
        ArrowLeft: () => go(prev),
        ArrowRight: () => go(next),
        ArrowUp: () => go(trail[trail.length - 1]),
        ArrowDown: () => go(nodes.find((n) => n.parent_id === node?.id)),
      };
      const run = handled[e.key];
      if (!run) return;
      e.preventDefault();
      e.stopPropagation();
      run();
    };
    addEventListener("keydown", onKey, true);
    return () => removeEventListener("keydown", onKey, true);
  }, [nodes]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 });
  }, [id]);

  if (!node) return null;
  return (
    <div
      className="focus-reader"
      role="dialog"
      aria-modal="true"
      aria-label={`专注阅读：${node.title}`}
      tabIndex={-1}
      ref={dialog}
    >
      <header className="focus-bar">
        <nav className="focus-trail" aria-label="所在位置">
          {trail.map((n) => (
            <span key={n.id}>
              <button onClick={() => setId(n.id)} title={n.title}>
                <AnalysisText text={n.title} />
              </button>
              <Icon name="chevron" size={12} />
            </span>
          ))}
          <strong title={node.title}>
            <AnalysisText text={node.title} />
          </strong>
        </nav>
        {index >= 0 && (
          <span
            className="focus-progress"
            aria-label={`第 ${index + 1} 个，共 ${order.length} 个`}
          >
            {index + 1} / {order.length}
            <i style={{ width: `${((index + 1) / order.length) * 100}%` }} />
          </span>
        )}
        <button
          className="icon-button small"
          aria-label="退出专注阅读"
          title="返回导图（Esc）"
          onClick={() => onClose(node.id)}
        >
          <Icon name="close" size={16} />
        </button>
      </header>
      <div className="focus-scroll" ref={scroller}>
        <article className="focus-col" key={node.id}>
          <NodeDetails
            node={node}
            report={report}
            onCollapse={() => undefined}
            isCollapsed={false}
            hasChildren={false}
            onNavigate={setId}
          />
          {children.length > 0 && (
            <section className="focus-children">
              <div className="section-title">
                <h3>展开来看</h3>
                <span>{children.length} 个下级论点</span>
              </div>
              <div className="focus-cards">
                {children.map((c) => (
                  <button key={c.id} onClick={() => setId(c.id)}>
                    <span className="detail-type">
                      <span className={`kind-dot kind-${c.kind}`} />
                      {kindLabels[c.kind]}
                    </span>
                    <strong>
                      <AnalysisText text={c.title} />
                    </strong>
                    <small>
                      <AnalysisText text={c.summary} />
                    </small>
                  </button>
                ))}
              </div>
            </section>
          )}
        </article>
      </div>
      <footer className="focus-foot">
        <button disabled={!prev} onClick={() => prev && setId(prev.id)}>
          <kbd>←</kbd>
          <span>
            <small>上一个</small>
            <AnalysisText text={prev?.title || "已是开头"} />
          </span>
        </button>
        <span className="focus-keys">
          <kbd>↑</kbd> 上级 · <kbd>↓</kbd> 下级 · <kbd>Esc</kbd> 回到导图
        </span>
        <button
          className="is-next"
          disabled={!next}
          onClick={() => next && setId(next.id)}
        >
          <span>
            <small>下一个</small>
            <AnalysisText text={next?.title || "已读到结尾"} />
          </span>
          <kbd>→</kbd>
        </button>
      </footer>
    </div>
  );
}
