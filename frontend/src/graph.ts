import mermaid from "mermaid";
import { standaloneSvg } from "./export";
import type {
  DashboardReport,
  GraphView,
  GraphValidation,
  NodeKind,
  PaperEdge,
} from "./types";

export const kindLabels: Record<NodeKind, string> = {
  theme: "研究主题",
  question: "核心问题",
  method: "研究方法",
  evidence: "证据与实验",
  contribution: "关键贡献",
  conclusion: "研究结论",
  limitation: "局限与边界",
  concept: "概念",
  background: "研究背景",
};
export const relationLabels: Record<string, string> = {
  motivates: "引出",
  uses: "使用",
  supports: "支持",
  derives: "推出",
  limits: "限制",
  explains: "解释",
  contains: "包含",
};

const escape = (text: string) =>
  text.replace(/[&"<>\n\r\[\]{}|`#;]/g, (c) => `#${c.charCodeAt(0)};`);

/**
 * Wraps a title into at most three balanced lines without splitting Latin
 * words ("Transformer" stays whole). CJK glyphs count as one unit, Latin
 * characters as ~0.56. Text never becomes a Mermaid instruction or HTML.
 */
export function wrapTitle(text: string, width = 11, maxLines = 3): string[] {
  const tokens =
    text
      .trim()
      .match(
        /\$[^$]+\$|(?:\\[A-Za-z]+|[A-Za-z])(?:[_^]\{[^{}]+\})+|[A-Za-z0-9.+\-_/%']+|\s+|./gsu,
      ) || [];
  const size = (t: string) =>
    [...t].reduce((n, c) => n + (c.charCodeAt(0) < 0x2e80 ? 0.56 : 1), 0);
  // Aim every line at the same length so short tails ("…实验证/据") don't dangle.
  const total = size(text.trim());
  const target = total / Math.min(maxLines, Math.max(1, Math.ceil(total / width)));
  const limit = Math.min(width, target + 0.8);
  const lines: string[] = [];
  let line = "";
  for (const token of tokens) {
    if (/^\s+$/.test(token)) {
      if (line) line += " ";
      continue;
    }
    if (line && size(line + token) > limit) {
      lines.push(line.trim());
      line = "";
    }
    line += token;
  }
  if (line.trim()) lines.push(line.trim());
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines);
    kept[maxLines - 1] = `${[...kept[maxLines - 1]].slice(0, -1).join("")}…`;
    return kept;
  }
  return lines;
}

const nodeLabel = (title: string) => wrapTitle(title).map(escape).join("<br>");

/** Parent → child links form the layout skeleton; the rest are relations. */
export function isTreeEdge(report: DashboardReport, edge: PaperEdge): boolean {
  return (
    report.analysis.nodes.find((n) => n.id === edge.target)?.parent_id === edge.source
  );
}

export function childCounts(
  report: DashboardReport,
  graph: GraphView,
): Map<string, number> {
  const inView = new Set(graph.node_ids);
  const counts = new Map<string, number>();
  for (const node of report.analysis.nodes)
    if (node.parent_id && inView.has(node.id) && inView.has(node.parent_id))
      counts.set(node.parent_id, (counts.get(node.parent_id) || 0) + 1);
  return counts;
}

export function visibleNodeIds(
  report: DashboardReport,
  graph: GraphView,
  collapsed: Set<string>,
): Set<string> {
  const parents = new Map(report.analysis.nodes.map((n) => [n.id, n.parent_id]));
  return new Set(
    graph.node_ids.filter((id) => {
      const visited = new Set<string>();
      let parent = parents.get(id);
      while (parent && !visited.has(parent)) {
        if (collapsed.has(parent)) return false;
        visited.add(parent);
        parent = parents.get(parent);
      }
      return true;
    }),
  );
}

/** Relations drawn on top of the tree (never part of the layout). */
export function crossEdges(
  report: DashboardReport,
  graph: GraphView,
  visible: Set<string>,
): PaperEdge[] {
  return report.analysis.edges.filter(
    (e) =>
      graph.edge_ids.includes(e.id) &&
      visible.has(e.source) &&
      visible.has(e.target) &&
      !isTreeEdge(report, e),
  );
}

/**
 * The on-screen source lays out the hierarchy only, so dagre produces a calm
 * tree; cross relations are overlaid on demand. `relations: true` produces the
 * complete source used for export.
 */
export function compileGraph(
  report: DashboardReport,
  graph: GraphView,
  collapsed: Set<string> = new Set(),
  { relations = false }: { relations?: boolean } = {},
): string {
  const visible = visibleNodeIds(report, graph, collapsed);
  const nodes = report.analysis.nodes.filter((n) => visible.has(n.id));
  const edges = report.analysis.edges.filter(
    (e) =>
      graph.edge_ids.includes(e.id) &&
      visible.has(e.source) &&
      visible.has(e.target) &&
      (relations || isTreeEdge(report, e)),
  );
  const lines = ["flowchart LR"];
  for (const node of nodes)
    lines.push(
      `n${node.id}["${nodeLabel(node.title)}"]`,
      `class n${node.id} pl_${node.id};`,
      `class n${node.id} kind_${node.kind};`,
    );
  for (const edge of edges) {
    const tree = isTreeEdge(report, edge);
    const label =
      relations && !tree && edge.label
        ? `|"${escape([...edge.label].slice(0, 14).join(""))}"|`
        : "";
    lines.push(
      `n${edge.source} ${edge.relation === "limits" ? "-.->" : "-->"}${label} n${edge.target}`,
    );
  }
  // Deliberately no click callbacks, links, directives or untrusted style declarations.
  return lines.join("\n");
}

let renderCounter = 0;
let queue: Promise<unknown> = Promise.resolve();
/** Colours come from CSS variables, so a theme switch never re-renders. */
export function renderGraph(source: string): Promise<string> {
  const job = queue.then(async () => {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      htmlLabels: false,
      theme: "base",
      suppressErrorRendering: true,
      fontFamily: "Inter, Segoe UI, PingFang SC, Microsoft YaHei, system-ui, sans-serif",
      maxTextSize: 300000,
      maxEdges: 2000,
      themeVariables: {
        primaryColor: "#ffffff",
        primaryTextColor: "#1f2328",
        primaryBorderColor: "#d9d8d3",
        lineColor: "#c4c3be",
        fontSize: "14px",
        edgeLabelBackground: "#ffffff",
      },
      flowchart: {
        htmlLabels: false,
        curve: "monotoneX",
        nodeSpacing: 18,
        rankSpacing: 64,
        padding: 14,
        wrappingWidth: 400,
        useMaxWidth: false,
      },
    });
    await mermaid.parse(source);
    const id = `plgraph${++renderCounter}`;
    try {
      const result = await mermaid.render(id, source);
      return result.svg;
    } finally {
      document.getElementById(`d${id}`)?.remove();
    }
  });
  queue = job.catch(() => undefined);
  return job;
}

/** Batch files use Mermaid's light palette, independent of the reader theme. */
function standaloneGraph(markup: string): string {
  const host = document.createElement("div");
  host.style.cssText =
    "all:initial;position:absolute;left:0;top:0;opacity:0;pointer-events:none;--bg:#ffffff";
  host.setAttribute("aria-hidden", "true");
  // Keep Mermaid's embedded stylesheet, but exclude application selectors while
  // measuring/freezing it. display:none would make SVG measurements empty.
  const root = host.attachShadow({ mode: "closed" });
  const container = document.createElement("div");
  root.append(container);
  document.body.append(host);
  try {
    container.innerHTML = markup;
    const svg = container.querySelector("svg");
    if (!svg) throw new Error("导图渲染未生成 SVG");
    return standaloneSvg(svg).text;
  } finally {
    host.remove();
  }
}

export async function validateAll(
  report: DashboardReport,
): Promise<{ graphs: GraphValidation[]; errors: string[] }> {
  const graphs: GraphValidation[] = [];
  for (const graph of report.graphs) {
    try {
      graphs.push({
        id: graph.id,
        valid: true,
        svg: standaloneGraph(
          await renderGraph(graph.mermaid || compileGraph(report, graph)),
        ),
      });
    } catch (error) {
      graphs.push({ id: graph.id, valid: false, error: String(error) });
    }
  }
  return {
    graphs,
    errors: graphs.filter((g) => !g.valid).map((g) => `${g.id}: ${g.error}`),
  };
}
