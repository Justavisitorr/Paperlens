import type { Prefs } from "./prefs";
import type { DashboardReport, GraphView } from "./types";

/**
 * Opening fold: the complete argument, and any other big view, starts at its
 * first level so the spine reads first.
 */
const AUTO_FOLD_LIMIT = 30;
const AUTO_FOLD_VIEWS = new Set(["argument"]);
export function initialFold(
  report: DashboardReport,
  graph: GraphView | undefined,
  depth: Prefs["mapDepth"],
): Set<string> {
  if (!graph || depth === "all") return new Set();
  if (
    depth === "auto" &&
    !AUTO_FOLD_VIEWS.has(graph.id) &&
    graph.node_ids.length <= AUTO_FOLD_LIMIT
  )
    return new Set();
  const inView = new Set(graph.node_ids);
  const parent = new Map(report.analysis.nodes.map((n) => [n.id, n.parent_id]));
  const parents = new Set(
    graph.node_ids
      .map((id) => parent.get(id))
      .filter((p): p is string => !!p && inView.has(p)),
  );
  // Keep roots and their direct children open; fold every deeper branch.
  return new Set(
    graph.node_ids.filter((id) => {
      const up = parent.get(id);
      return parents.has(id) && !!up && inView.has(up);
    }),
  );
}
