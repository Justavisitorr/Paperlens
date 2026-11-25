import { describe, expect, it, vi } from "vitest";

// The graph helpers under test are pure; the renderer is never called here.
vi.mock("mermaid", () => ({ default: {} }));

import { initialFold } from "./fold";
import { compileGraph, visibleNodeIds, wrapTitle } from "./graph";
import { href, parseRoute } from "./router";
import type { DashboardReport, GraphView } from "./types";

/** A report with just the fields the helpers read. */
function tree(parents: Record<string, string | null>, viewId = "argument") {
  const ids = Object.keys(parents);
  const edges = ids
    .filter((id) => parents[id])
    .map((id) => ({
      id: `e_${id}`,
      source: parents[id],
      target: id,
      relation: "supports",
    }));
  const report = {
    analysis: {
      nodes: ids.map((id) => ({
        id,
        parent_id: parents[id],
        title: `论点 ${id}`,
        kind: "claim",
      })),
      edges,
    },
  } as unknown as DashboardReport;
  const graph = {
    id: viewId,
    node_ids: ids,
    edge_ids: edges.map((e) => e.id),
  } as unknown as GraphView;
  return { report, graph };
}

const depth1 = { r: null, a: "r", b: "r" };
const depth2 = { r: null, a: "r", b: "r", a1: "a", a2: "a", b1: "b" };
const depth3 = { ...depth2, a1x: "a1" };

describe("initialFold", () => {
  it("keeps a one-level tree fully open", () => {
    const { report, graph } = tree(depth1);
    expect([...initialFold(report, graph, "auto")]).toEqual([]);
  });

  it("folds the argument view to its first level", () => {
    const { report, graph } = tree(depth2);
    const folded = initialFold(report, graph, "auto");
    expect([...folded].sort()).toEqual(["a", "b"]);
    expect([...visibleNodeIds(report, graph, folded)].sort()).toEqual(["a", "b", "r"]);
  });

  it("folds only branches that have children, at every depth", () => {
    const { report, graph } = tree(depth3);
    expect([...initialFold(report, graph, "auto")].sort()).toEqual(["a", "a1", "b"]);
  });

  it("leaves small views other than the argument open", () => {
    const { report, graph } = tree(depth3, "overview");
    expect(initialFold(report, graph, "auto").size).toBe(0);
  });

  it("honours an explicit depth preference", () => {
    const { report, graph } = tree(depth3, "overview");
    expect(initialFold(report, graph, "all").size).toBe(0);
    expect(initialFold(report, graph, "first").size).toBe(3);
  });

  it("tolerates a missing view", () => {
    const { report } = tree(depth1);
    expect(initialFold(report, undefined, "auto").size).toBe(0);
  });
});

describe("wrapTitle", () => {
  it("balances Chinese titles over at most three lines", () => {
    const lines = wrapTitle("掩码语言模型使双向表示的预训练成为可能");
    expect(lines.length).toBeGreaterThan(1);
    expect(lines.length).toBeLessThanOrEqual(3);
    expect(lines.join("")).toBe("掩码语言模型使双向表示的预训练成为可能");
  });

  it("never splits a Latin word", () => {
    const lines = wrapTitle("BERT 在 GLUE 上取得 80.5% 的平均分");
    expect(lines.join(" ")).toContain("80.5%");
    for (const word of ["BERT", "GLUE"])
      expect(lines.some((l) => l.includes(word))).toBe(true);
  });

  it("truncates with an ellipsis past the line limit", () => {
    const lines = wrapTitle("一".repeat(80));
    expect(lines).toHaveLength(3);
    expect(lines[2].endsWith("…")).toBe(true);
  });
});

describe("compileGraph", () => {
  it("emits only the visible tree, without callbacks or links", () => {
    const { report, graph } = tree(depth2);
    const source = compileGraph(report, graph, new Set(["a"]));
    expect(source.startsWith("flowchart LR")).toBe(true);
    expect(source).toContain("nr --> na");
    expect(source).not.toContain("na1");
    expect(source).not.toMatch(/\bclick\b|href|%%\{/);
  });

  it("adds labelled cross relations only for export", () => {
    const { report, graph } = tree(depth1);
    report.analysis.edges.push({
      id: "x",
      source: "a",
      target: "b",
      relation: "limits",
      label: "限制",
    } as never);
    graph.edge_ids.push("x");
    expect(compileGraph(report, graph)).not.toContain("na -.->");
    expect(compileGraph(report, graph, new Set(), { relations: true })).toContain(
      'na -.->|"限制"| nb',
    );
  });

  it("escapes quotes and markup in titles", () => {
    const { report, graph } = tree(depth1);
    report.analysis.nodes[1].title = '"<b>注入</b>"';
    const source = compileGraph(report, graph);
    expect(source).not.toContain("<b>");
    const line = source.split("\n").find((l) => l.startsWith("na[")) || "";
    // Only the two delimiting quotes remain; the title's own quotes are entities.
    expect(line.slice(4, -2)).not.toContain('"');
  });
});

describe("routes", () => {
  it("parses every page and falls back to the map mode", () => {
    expect(parseRoute("#/library")).toEqual({ page: "library" });
    expect(parseRoute("#/read/abc/glossary")).toEqual({
      page: "read",
      paperId: "abc",
      mode: "glossary",
    });
    expect(parseRoute("#/read/abc/unknown")).toMatchObject({ mode: "overview" });
    expect(parseRoute("#/nowhere")).toBeNull();
    expect(parseRoute("")).toBeNull();
  });

  it("round-trips ids that need encoding", () => {
    const route = { page: "read" as const, paperId: "论文 1/2", mode: "text" as const };
    expect(parseRoute(href(route))).toEqual(route);
    expect(href({ ...route, mode: "map" })).toBe(
      `#/read/${encodeURIComponent("论文 1/2")}/map`,
    );
  });
});
