import { AnalysisText } from "./AnalysisText";
import { useMemo, useRef, useState } from "react";
import { Icon } from "./icons";
import type { DashboardReport, PaperNode } from "./types";

interface Row {
  node: PaperNode;
  depth: number;
  hasChildren: boolean;
}

/**
 * The paper's argument as a keyboard-navigable tree. It shares its folded
 * set and selection with the map, so both always tell the same story.
 */
export function Outline({
  report,
  collapsed,
  selected,
  onToggle,
  onSelect,
}: {
  report: DashboardReport;
  collapsed: Set<string>;
  selected: string | null;
  onToggle: (id: string) => void;
  onSelect: (id: string) => void;
}) {
  const [cursor, setCursor] = useState<string | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const { roots, children } = useMemo(() => {
    const ids = new Set(report.analysis.nodes.map((n) => n.id));
    const children = new Map<string, PaperNode[]>();
    const roots: PaperNode[] = [];
    for (const node of report.analysis.nodes) {
      if (node.parent_id && ids.has(node.parent_id))
        children.set(node.parent_id, [...(children.get(node.parent_id) || []), node]);
      else roots.push(node);
    }
    // A single theme root repeats the paper title; start from its branches.
    if (roots.length === 1 && children.get(roots[0].id)?.length)
      return { roots: children.get(roots[0].id)!, children };
    return { roots, children };
  }, [report]);
  const rows = useMemo(() => {
    const out: Row[] = [];
    const walk = (nodes: PaperNode[], depth: number) => {
      for (const node of nodes) {
        const kids = children.get(node.id) || [];
        out.push({ node, depth, hasChildren: kids.length > 0 });
        if (kids.length && !collapsed.has(node.id)) walk(kids, depth + 1);
      }
    };
    walk(roots, 0);
    return out;
  }, [roots, children, collapsed]);

  const current =
    cursor && rows.some((r) => r.node.id === cursor)
      ? cursor
      : selected && rows.some((r) => r.node.id === selected)
        ? selected
        : rows[0]?.node.id;
  const move = (id: string | undefined) => {
    if (!id) return;
    setCursor(id);
    list.current?.querySelector<HTMLElement>(`[data-row="${CSS.escape(id)}"]`)?.focus();
  };
  const onKey = (event: React.KeyboardEvent, row: Row, index: number) => {
    const id = row.node.id;
    const open = row.hasChildren && !collapsed.has(id);
    switch (event.key) {
      case "ArrowDown":
        move(rows[index + 1]?.node.id);
        break;
      case "ArrowUp":
        move(rows[index - 1]?.node.id);
        break;
      case "ArrowRight":
        if (row.hasChildren && !open) onToggle(id);
        else if (open) move(rows[index + 1]?.node.id);
        break;
      case "ArrowLeft":
        if (open) onToggle(id);
        else if (row.node.parent_id) move(row.node.parent_id);
        break;
      case "Home":
        move(rows[0]?.node.id);
        break;
      case "End":
        move(rows[rows.length - 1]?.node.id);
        break;
      case "Enter":
      case " ":
        onSelect(id);
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  return (
    <div className="outline" role="tree" aria-label="论点目录" ref={list}>
      {rows.map((row, index) => {
        const { node } = row;
        return (
          <div
            key={node.id}
            role="treeitem"
            data-row={node.id}
            aria-level={row.depth + 1}
            aria-selected={selected === node.id}
            aria-expanded={row.hasChildren ? !collapsed.has(node.id) : undefined}
            tabIndex={node.id === current ? 0 : -1}
            className={`outline-row ${selected === node.id ? "is-selected" : ""}`}
            style={{ "--depth": row.depth } as React.CSSProperties}
            onClick={() => {
              setCursor(node.id);
              onSelect(node.id);
            }}
            onKeyDown={(e) => onKey(e, row, index)}
            title={node.title}
          >
            {row.hasChildren ? (
              <span
                className={`twisty ${collapsed.has(node.id) ? "" : "is-open"}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onToggle(node.id);
                }}
                aria-hidden="true"
              >
                <Icon name="chevron" size={12} />
              </span>
            ) : (
              <span className="twisty" aria-hidden="true" />
            )}
            <span className={`kind-dot kind-${node.kind}`} aria-hidden="true" />
            <span className="outline-title">
              <AnalysisText text={node.title} />
            </span>
            {node.evidence_ids.length > 0 && (
              <small aria-label={`${node.evidence_ids.length} 条证据`}>
                {node.evidence_ids.length}
              </small>
            )}
          </div>
        );
      })}
    </div>
  );
}
