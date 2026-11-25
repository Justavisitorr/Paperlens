import { analysisSvgFragment } from "./AnalysisText";
import { wrapTitle } from "./graph";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "./icons";
import { crossEdges, kindLabels, relationLabels, renderGraph } from "./graph";
import { reducedMotion } from "./prefs";
import { isTyping } from "./Shortcuts";
import type { DashboardReport, GraphView, NodeKind, PaperEdge } from "./types";

interface Props {
  report: DashboardReport;
  graph: GraphView;
  source: string;
  visible: Set<string>;
  collapsed: Set<string>;
  childCount: Map<string, number>;
  selected: string | null;
  matches: Set<string>;
  focus: { id: string; tick: number } | null;
  /** Node whose on-screen position must survive the next re-render. */
  anchorId: string | null;
  showRelations: boolean;
  onShowRelations: (show: boolean) => void;
  onSelect: (id: string) => void;
  /** Enter on a focused node: select it, or open it for focused reading. */
  onActivate: (id: string) => void;
  onToggle: (id: string) => void;
  /** Arrow key on a focused node; returns the node that should take focus. */
  onStep: (id: string, key: string) => string | null;
  onCollapseAll: () => void;
  onExpandAll: () => void;
  onReady: (svg: SVGSVGElement | null) => void;
}
interface Transform {
  x: number;
  y: number;
  scale: number;
}
const SVG = "http://www.w3.org/2000/svg";
const MIN = 0.12;
const MAX = 2.5;
const clampScale = (s: number) => Math.max(MIN, Math.min(MAX, s));
/** Scale at which 14px node labels stay about 10px on screen. */
const READABLE = 0.7;

function svgEl<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number>,
): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

/** Box of `el` in the coordinate system of `space`. */
function boxIn(el: SVGGraphicsElement, space: SVGGraphicsElement) {
  const b = el.getBBox();
  const a = space.getScreenCTM();
  const m = el.getScreenCTM();
  if (!a || !m) return null;
  const t = a.inverse().multiply(m);
  const p1 = new DOMPoint(b.x, b.y).matrixTransform(t);
  const p2 = new DOMPoint(b.x + b.width, b.y + b.height).matrixTransform(t);
  return { x: p1.x, y: p1.y, w: p2.x - p1.x, h: p2.y - p1.y };
}

function screenCenter(el: Element) {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

/** Centre of `el` in the untransformed pixels of the drawing layer. */
function drawingCenter(el: Element, drawing: HTMLElement) {
  const d = drawing.getBoundingClientRect();
  const k = d.width / (drawing.offsetWidth || 1) || 1;
  const c = screenCenter(el);
  return { x: (c.x - d.left) / k, y: (c.y - d.top) / k };
}

/**
 * Dagre's spline edges may cut through siblings when a parent sits far from
 * its children. Tree edges are redrawn as right-to-left S-curves that stay in
 * the empty gap between two columns, so they can never cross a box.
 */
function routeTreeEdges(element: SVGSVGElement) {
  const paths = [
    ...element.querySelectorAll<SVGPathElement>("path.flowchart-link[data-source]"),
  ];
  const space = paths[0]?.parentNode as SVGGraphicsElement | null;
  if (!space) return;
  const boxes = new Map<string, { x: number; y: number; w: number; h: number }>();
  element.querySelectorAll<SVGGElement>("g.node[data-node-id]").forEach((node) => {
    const rect = node.querySelector<SVGGraphicsElement>(
      ":scope > rect, :scope > .label-container",
    );
    const box = rect && boxIn(rect, space);
    if (box) boxes.set(node.dataset.nodeId || "", box);
  });
  const all = [...boxes.values()];
  const column = (b: { x: number; w: number }) =>
    all.filter((o) => Math.abs(o.x + o.w / 2 - (b.x + b.w / 2)) < 2);
  for (const path of paths) {
    const a = boxes.get(path.dataset.source || "");
    const b = boxes.get(path.dataset.target || "");
    if (!a || !b || b.x <= a.x + a.w) continue;
    // Keep Mermaid's gap between the line end and the box, which the arrow head fills.
    let inset = 0;
    try {
      inset = b.x - path.getPointAtLength(path.getTotalLength()).x;
    } catch {
      /* not rendered yet */
    }
    inset = Math.max(0, Math.min(12, inset));
    const x0 = a.x + a.w;
    const y0 = a.y + a.h / 2;
    const x3 = b.x - inset;
    const y3 = b.y + b.h / 2;
    const g1 = Math.max(x0, ...column(a).map((o) => o.x + o.w));
    const g2 = Math.max(g1, Math.min(x3, ...column(b).map((o) => o.x - inset)));
    const k = (g2 - g1) / 2;
    path.setAttribute(
      "d",
      Math.abs(y3 - y0) < 1
        ? `M${x0},${y0} L${x3},${y3}`
        : `M${x0},${y0} L${g1},${y0} C${g1 + k},${y0} ${g2 - k},${y3} ${g2},${y3} L${x3},${y3}`,
    );
  }
}

export function GraphCanvas(props: Props) {
  const {
    report,
    graph,
    source,
    visible,
    collapsed,
    selected,
    matches,
    focus,
    showRelations,
  } = props;
  const viewport = useRef<HTMLDivElement>(null);
  const drawing = useRef<HTMLDivElement>(null);
  // A press only becomes a pan after a few pixels, so nodes stay clickable
  // and a drag that starts on a node never selects its text.
  const press = useRef<{
    x: number;
    y: number;
    tx: number;
    ty: number;
    dragging: boolean;
  } | null>(null);
  const swallowClick = useRef(false);
  const wantFocus = useRef<string | null>(null);
  const pointerInside = useRef(true);
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [smooth, setSmooth] = useState(false);
  const [hover, setHover] = useState<string | null>(null);
  const [transform, setTransform] = useState<Transform>({
    x: 0,
    y: 0,
    scale: 1,
  });
  const size = useRef({ width: 1, height: 1 });
  const moved = useRef(false);
  const previous = useRef<{ view: string; ids: Set<string> }>({
    view: "",
    ids: new Set(),
  });
  const anchor = useRef<{ id: string; x: number; y: number } | null>(null);
  const latest = useRef(props);
  latest.current = props;
  const smoothTimer = useRef(0);
  const mountedAt = useRef(Date.now());
  const rendered = useRef("");
  const focusDone = useRef(0);

  const animateTo = useCallback((next: Transform | ((t: Transform) => Transform)) => {
    if (!reducedMotion()) {
      setSmooth(true);
      clearTimeout(smoothTimer.current);
      smoothTimer.current = window.setTimeout(() => setSmooth(false), 420);
    }
    setTransform(next);
  }, []);

  /**
   * `whole` shows the entire drawing however small it gets (the 适应窗口
   * control). Automatic fits never shrink labels below a readable size; a
   * taller tree then overflows and is read by scrolling.
   */
  const fit = useCallback(
    (animate = false, whole = false) => {
      const rect = viewport.current?.getBoundingClientRect();
      if (!rect || !rect.width) return;
      const { width, height } = size.current;
      const all = Math.min(1.1, (rect.width - 64) / width, (rect.height - 96) / height);
      const scale = clampScale(whole ? all : Math.max(all, READABLE));
      const w = width * scale;
      const next = {
        scale,
        // Overflowing sideways, keep the root (left edge) in view.
        x: w > rect.width - 64 ? 32 : (rect.width - w) / 2,
        y: (rect.height - height * scale) / 2 - 12,
      };
      moved.current = false;
      if (animate) animateTo(next);
      else setTransform(next);
    },
    [animateTo],
  );

  const focusWanted = () => {
    const id = wantFocus.current;
    const node = id
      ? drawing.current?.querySelector<SVGGElement>(
          `g.node[data-node-id="${CSS.escape(id)}"]`,
        )
      : null;
    if (!node) return;
    node.focus({ preventScroll: true });
    if (document.activeElement === node) wantFocus.current = null;
  };

  /* ---------- render ---------- */
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    window.__paperlensReady = false;
    renderGraph(source)
      .then((result) => {
        if (!active) return;
        const root = drawing.current;
        const id = latest.current.anchorId;
        const old = id
          ? root?.querySelector(`g.node[data-node-id="${CSS.escape(id)}"]`)
          : null;
        anchor.current = old && id ? { id, ...screenCenter(old) } : null;
        rendered.current = source;
        setSvg(result);
        setLoading(false);
      })
      .catch((reason) => {
        if (!active) return;
        const message = String(reason);
        setError(message);
        setLoading(false);
        window.__paperlensErrors.push(message);
        window.__paperlensReady = true;
      });
    return () => {
      active = false;
    };
  }, [source]);

  /* ---------- decorate the fresh SVG ---------- */
  useLayoutEffect(() => {
    const element = drawing.current?.querySelector("svg");
    if (!element) return;
    const { report, graph, childCount, collapsed } = latest.current;
    const viewBox = element.viewBox.baseVal;
    size.current = { width: viewBox.width, height: viewBox.height };
    element.setAttribute("width", String(viewBox.width));
    element.setAttribute("height", String(viewBox.height));
    element.style.maxWidth = "none";
    element.setAttribute(
      "aria-label",
      "导图，使用 Tab 选择节点，回车查看详情，再按回车专注阅读",
    );
    const sameView = previous.current.view === graph.id;
    const ids = new Set<string>();
    element.querySelectorAll<SVGGElement>("g.node").forEach((node) => {
      // Older generated reports may contain comma-separated classes in one token.
      const classes = [...node.classList].flatMap((c) => c.split(","));
      classes.forEach((name) => node.classList.add(name));
      const id = classes.find((c) => c.startsWith("pl_"))?.slice(3);
      const data = report.analysis.nodes.find((n) => n.id === id);
      if (!id || !data) return;
      ids.add(id);
      const mathLabel = node.querySelector(".label text");
      if (mathLabel) {
        const first = mathLabel.querySelector("tspan");
        const initialY = first?.getAttribute("y");
        mathLabel.replaceChildren();
        wrapTitle(data.title).forEach((line, index) => {
          const row = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
          row.setAttribute("x", "0");
          row.setAttribute("dy", "1.1em");
          if (!index && initialY) row.setAttribute("y", initialY);
          row.append(analysisSvgFragment(line));
          mathLabel.append(row);
        });
      }
      node.setAttribute("data-node-id", id);
      node.setAttribute("tabindex", "0");
      node.setAttribute("role", "button");
      node.setAttribute("aria-label", `${kindLabels[data.kind]}：${data.title}`);
      node.onclick = () => latest.current.onSelect(id);
      node.onkeydown = (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          latest.current.onActivate(id);
        } else if (event.key === " ") {
          event.preventDefault();
          latest.current.onSelect(id);
        } else if (event.key.startsWith("Arrow")) {
          event.preventDefault();
          event.stopPropagation();
          wantFocus.current = latest.current.onStep(id, event.key);
          focusWanted();
        }
      };
      node.onpointerenter = () => setHover(id);
      node.onpointerleave = () => setHover((h) => (h === id ? null : h));
      if (sameView && previous.current.ids.size && !previous.current.ids.has(id))
        node.classList.add("is-new");
      const rect = node.querySelector<SVGRectElement>(":scope > rect");
      if (!rect) return;
      rect.setAttribute("rx", "7");
      rect.setAttribute("ry", "7");
      const x = Number(rect.getAttribute("x"));
      const y = Number(rect.getAttribute("y"));
      const w = Number(rect.getAttribute("width"));
      const h = Number(rect.getAttribute("height"));
      node.insertBefore(
        svgEl("rect", {
          class: "pl-kind",
          x: x + 5,
          y: y + 8,
          width: 3,
          height: Math.max(4, h - 16),
          rx: 1.5,
        }),
        rect.nextSibling,
      );
      const count = childCount.get(id) || 0;
      if (!count) return;
      const folded = collapsed.has(id);
      const chip = svgEl("g", {
        class: `pl-chip${folded ? " is-folded" : ""}`,
        transform: `translate(${x + w}, ${y + h / 2})`,
        "aria-hidden": "true",
      });
      const label = folded ? String(count) : "−";
      chip.append(
        svgEl("rect", {
          x: folded ? -11 - (label.length - 1) * 3.5 : -8,
          y: -8,
          width: folded ? 22 + (label.length - 1) * 7 : 16,
          height: 16,
          rx: 8,
        }),
      );
      const text = svgEl("text", { x: 0, y: 0.5 });
      text.textContent = label;
      chip.append(text);
      const title = svgEl("title", {});
      title.textContent = folded ? `展开 ${count} 个下级论点` : "折叠下级论点";
      chip.append(title);
      chip.addEventListener("click", (event) => {
        event.stopPropagation();
        latest.current.onToggle(id);
      });
      chip.addEventListener("pointerdown", (event) => event.stopPropagation());
      node.append(chip);
    });

    // Tag edges with their endpoints for neighbourhood highlighting.
    const edges = report.analysis.edges.filter((e) => graph.edge_ids.includes(e.id));
    element.querySelectorAll<SVGPathElement>("path.flowchart-link").forEach((path) => {
      const key = path.dataset.id || path.id.replace(/^plgraph\d+-/, "");
      const edge = edges.find((e) => key.startsWith(`L_n${e.source}_n${e.target}_`));
      if (!edge) return;
      path.dataset.source = edge.source;
      path.dataset.target = edge.target;
      if (
        !sameView &&
        !reducedMotion() &&
        !path.classList.contains("edge-pattern-dotted")
      ) {
        path.setAttribute("pathLength", "1");
        path.classList.add("draw-in");
      }
    });
    routeTreeEdges(element);

    // Keep the node the reader just folded/unfolded under their pointer.
    const held = anchor.current;
    const now = held
      ? element.querySelector(`g.node[data-node-id="${CSS.escape(held.id)}"]`)
      : null;
    if (held && now && sameView) {
      const c = screenCenter(now);
      setTransform((t) => ({ ...t, x: t.x + held.x - c.x, y: t.y + held.y - c.y }));
    } else if (!sameView || !moved.current) fit();
    anchor.current = null;
    previous.current = { view: graph.id, ids };
    focusWanted();
    latest.current.onReady(element);
    window.__paperlensReady = true;
    return () => latest.current.onReady(null);
  }, [svg, fit]);

  /* ---------- selection, neighbourhood and relation overlay ---------- */
  const active = selected || hover;
  useEffect(() => {
    const element = drawing.current?.querySelector("svg");
    if (!element) return;
    const near = new Set<string>();
    const relations: PaperEdge[] = crossEdges(report, graph, visible).filter(
      (e) => showRelations || e.source === active || e.target === active,
    );
    if (selected) {
      near.add(selected);
      const node = report.analysis.nodes.find((n) => n.id === selected);
      if (node?.parent_id) near.add(node.parent_id);
      report.analysis.nodes.forEach((n) => n.parent_id === selected && near.add(n.id));
      crossEdges(report, graph, visible).forEach((e) => {
        if (e.source === selected) near.add(e.target);
        if (e.target === selected) near.add(e.source);
      });
    }
    element.classList.toggle("has-selection", Boolean(selected));
    element.querySelectorAll<SVGGElement>("g.node[data-node-id]").forEach((node) => {
      const id = node.dataset.nodeId || "";
      node.classList.toggle("is-selected", id === selected);
      node.classList.toggle("is-match", matches.has(id));
      node.classList.toggle("is-near", near.has(id));
      node.setAttribute("aria-pressed", String(id === selected));
    });
    element.querySelectorAll<SVGPathElement>("path.flowchart-link").forEach((path) => {
      const { source: s, target: t } = path.dataset;
      path.classList.toggle(
        "is-near",
        Boolean(selected) && (s === selected || t === selected),
      );
    });

    element.querySelector("g.pl-relations")?.remove();
    if (!relations.length) return;
    const nodesGroup = element.querySelector<SVGGElement>("g.nodes");
    const space = (nodesGroup?.parentNode as SVGGElement | null) || element;
    const layer = svgEl("g", { class: "pl-relations" });
    const marker = `${element.id}-relation-arrow`;
    const defs = svgEl("defs", {});
    const arrow = svgEl("marker", {
      id: marker,
      viewBox: "0 0 10 10",
      refX: 8,
      refY: 5,
      markerWidth: 7,
      markerHeight: 7,
      orient: "auto-start-reverse",
    });
    arrow.append(svgEl("path", { d: "M0,1 L9,5 L0,9 z", class: "pl-relation-head" }));
    defs.append(arrow);
    layer.append(defs);
    if (nodesGroup) space.insertBefore(layer, nodesGroup);
    else space.append(layer);
    const box = (id: string) => {
      const el = element.querySelector<SVGGElement>(
        `g.node[data-node-id="${CSS.escape(id)}"]`,
      );
      return el ? boxIn(el, space) : null;
    };
    for (const edge of relations) {
      const a = box(edge.source);
      const b = box(edge.target);
      if (!a || !b) continue;
      const ac = { x: a.x + a.w / 2, y: a.y + a.h / 2 };
      const bc = { x: b.x + b.w / 2, y: b.y + b.h / 2 };
      const dx = bc.x - ac.x;
      let p: number[];
      if (Math.abs(dx) > (a.w + b.w) / 2 + 24) {
        const sign = Math.sign(dx);
        const x0 = sign > 0 ? a.x + a.w : a.x;
        const x3 = sign > 0 ? b.x : b.x + b.w;
        const k = Math.max(36, Math.abs(x3 - x0) / 2);
        p = [x0, ac.y, x0 + sign * k, ac.y, x3 - sign * k, bc.y, x3, bc.y];
      } else {
        // Same column: bow out to the right so the arc never crosses the tree.
        const x0 = a.x + a.w;
        const x3 = b.x + b.w;
        const bow = Math.max(x0, x3) + 48 + Math.abs(bc.y - ac.y) * 0.12;
        p = [x0, ac.y, bow, ac.y, bow, bc.y, x3, bc.y];
      }
      const focusEdge = edge.source === active || edge.target === active;
      const g = svgEl("g", {
        class: `pl-relation rel-${edge.relation}${focusEdge ? " is-focus" : ""}`,
      });
      const title = svgEl("title", {});
      title.textContent = edge.label || relationLabels[edge.relation] || edge.relation;
      g.append(
        title,
        svgEl("path", {
          d: `M${p[0]},${p[1]} C${p[2]},${p[3]} ${p[4]},${p[5]} ${p[6]},${p[7]}`,
          "marker-end": `url(#${marker})`,
        }),
      );
      const mx = (p[0] + 3 * p[2] + 3 * p[4] + p[6]) / 8;
      const my = (p[1] + 3 * p[3] + 3 * p[5] + p[7]) / 8;
      const chip = svgEl("g", {
        class: "pl-relation-label",
        transform: `translate(${mx}, ${my})`,
      });
      const text = svgEl("text", { x: 0, y: 0.5 });
      text.textContent = relationLabels[edge.relation] || edge.relation;
      const bg = svgEl("rect", { x: -16, y: -10, width: 32, height: 20, rx: 10 });
      chip.append(bg, text);
      g.append(chip);
      layer.append(g);
      const width = text.getComputedTextLength?.() || 24;
      bg.setAttribute("x", String(-width / 2 - 8));
      bg.setAttribute("width", String(width + 16));
    }
  }, [svg, report, graph, visible, selected, active, matches, showRelations]);

  /* ---------- focus a node (search, outline, related links) ---------- */
  useEffect(() => {
    // A request left over from before this canvas mounted (the reader went to
    // the full text and came back) must not pull a freshly fitted map aside.
    if (
      !focus ||
      focus.tick < mountedAt.current - 1000 ||
      focus.tick === focusDone.current
    )
      return;
    // Wait for the drawing that includes the unfolded path to the node.
    if (rendered.current !== latest.current.source) return;
    const layer = drawing.current;
    const node = layer?.querySelector(`g.node[data-node-id="${CSS.escape(focus.id)}"]`);
    const rect = viewport.current?.getBoundingClientRect();
    if (!layer || !node || !rect) return;
    focusDone.current = focus.tick;
    // Measured in drawing space, so a transform that is still pending (a fit
    // in the same commit) cannot skew the result.
    const c = drawingCenter(node, layer);
    animateTo((t) => {
      const scale = Math.max(t.scale, 0.85);
      return {
        scale,
        x: rect.width / 2 - c.x * scale,
        y: rect.height / 2 - c.y * scale,
      };
    });
    moved.current = true;
    focusWanted();
  }, [focus, svg, animateTo]);

  /* ---------- wheel + resize ---------- */
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      setSmooth(false);
      moved.current = true;
      if (event.ctrlKey || event.metaKey) {
        const rect = element.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        setTransform((old) => {
          const scale = clampScale(old.scale * Math.exp(-event.deltaY * 0.004));
          return {
            scale,
            x: x - ((x - old.x) * scale) / old.scale,
            y: y - ((y - old.y) * scale) / old.scale,
          };
        });
      } else {
        const dx = event.shiftKey ? event.deltaY : event.deltaX;
        const dy = event.shiftKey ? 0 : event.deltaY;
        setTransform((old) => ({ ...old, x: old.x - dx, y: old.y - dy }));
      }
    };
    element.addEventListener("wheel", wheel, { passive: false });
    const observer = new ResizeObserver(() => {
      if (!moved.current) fit();
    });
    observer.observe(element);
    return () => {
      element.removeEventListener("wheel", wheel);
      observer.disconnect();
    };
  }, [fit]);

  /* ---------- keyboard panning without having to focus the canvas first ---------- */
  useEffect(() => {
    const onPointer = (e: PointerEvent) => {
      pointerInside.current = Boolean(viewport.current?.contains(e.target as Node));
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || isTyping(e)) return;
      const target = e.target as Element;
      const onCanvas = viewport.current?.contains(target);
      if (!onCanvas && !(target === document.body && pointerInside.current)) return;
      if (target.closest?.("g.node, button")) return;
      const step = e.shiftKey ? 240 : 80;
      const delta = (
        {
          ArrowLeft: [step, 0],
          ArrowRight: [-step, 0],
          ArrowUp: [0, step],
          ArrowDown: [0, -step],
        } as Record<string, [number, number]>
      )[e.key];
      if (delta) {
        e.preventDefault();
        moved.current = true;
        animateTo((t) => ({ ...t, x: t.x + delta[0], y: t.y + delta[1] }));
      } else if (e.key === "+" || e.key === "=") zoom(1.2);
      else if (e.key === "-") zoom(1 / 1.2);
      else if (e.key === "0") fit(true, true);
    };
    addEventListener("pointerdown", onPointer, true);
    addEventListener("keydown", onKey);
    return () => {
      removeEventListener("pointerdown", onPointer, true);
      removeEventListener("keydown", onKey);
    };
  });

  const zoom = (factor: number) => {
    const rect = viewport.current?.getBoundingClientRect();
    if (!rect) return;
    moved.current = true;
    animateTo((old) => {
      const scale = clampScale(old.scale * factor);
      return {
        scale,
        x: rect.width / 2 - ((rect.width / 2 - old.x) * scale) / old.scale,
        y: rect.height / 2 - ((rect.height / 2 - old.y) * scale) / old.scale,
      };
    });
  };

  const kinds = [
    ...new Set(report.analysis.nodes.filter((n) => visible.has(n.id)).map((n) => n.kind)),
  ] as NodeKind[];

  return (
    <div
      className="graph-viewport"
      ref={viewport}
      data-testid="graph-viewport"
      onPointerDown={(event) => {
        swallowClick.current = false;
        if (event.button !== 0) return;
        if (
          (event.target as Element).closest(
            ".pl-chip, button, .canvas-tools, .canvas-error",
          )
        )
          return;
        press.current = {
          x: event.clientX,
          y: event.clientY,
          tx: transform.x,
          ty: transform.y,
          dragging: false,
        };
      }}
      onPointerMove={(event) => {
        const p = press.current;
        if (!p) return;
        const dx = event.clientX - p.x;
        const dy = event.clientY - p.y;
        if (!p.dragging) {
          if (Math.hypot(dx, dy) < 4) return;
          p.dragging = true;
          setSmooth(false);
          event.currentTarget.setPointerCapture(event.pointerId);
          event.currentTarget.classList.add("is-dragging");
          getSelection()?.removeAllRanges();
        }
        moved.current = true;
        setTransform((old) => ({ ...old, x: p.tx + dx, y: p.ty + dy }));
      }}
      onPointerUp={(event) => {
        if (press.current?.dragging) swallowClick.current = true;
        press.current = null;
        event.currentTarget.classList.remove("is-dragging");
      }}
      onPointerCancel={(event) => {
        press.current = null;
        event.currentTarget.classList.remove("is-dragging");
      }}
      onScroll={(event) => {
        // Tab focus scrolls this clipped box natively, which would carry the
        // legend and tools off their corners. Turn that scroll into a pan.
        const el = event.currentTarget;
        const { scrollLeft: dx, scrollTop: dy } = el;
        if (!dx && !dy) return;
        el.scrollLeft = 0;
        el.scrollTop = 0;
        moved.current = true;
        setSmooth(false);
        setTransform((t) => ({ ...t, x: t.x - dx, y: t.y - dy }));
      }}
      onFocus={(event) => {
        // Keyboard focus can land on a node panned out of view (scrolling
        // cannot reach above or left of the drawing), so pan it back in.
        const node = (event.target as Element).closest("g.node");
        if (!node) return;
        const box = event.currentTarget.getBoundingClientRect();
        const r = node.getBoundingClientRect();
        const shift = (lo: number, hi: number, min: number, max: number) =>
          lo < min ? min - lo : hi > max ? max - hi : 0;
        const dx = shift(r.left, r.right, box.left + 32, box.right - 32);
        const dy = shift(r.top, r.bottom, box.top + 32, box.bottom - 72);
        if (!dx && !dy) return;
        moved.current = true;
        animateTo((t) => ({ ...t, x: t.x + dx, y: t.y + dy }));
      }}
      onClickCapture={(event) => {
        // The click that ends a pan must not select the node under the pointer.
        if (!swallowClick.current) return;
        swallowClick.current = false;
        event.stopPropagation();
      }}
      tabIndex={-1}
    >
      {loading && !svg && (
        <div className="canvas-notice">
          <span className="spinner" />
          正在排列导图…
        </div>
      )}
      {error && (
        <div className="canvas-error" role="alert">
          <Icon name="warning" />
          <strong>导图暂时无法显示</strong>
          <p>完整分析与原文证据仍可从左侧目录与“全文”查看。</p>
          <details>
            <summary>查看错误</summary>
            <pre>{error}</pre>
          </details>
        </div>
      )}
      <div
        className={`graph-drawing ${smooth ? "is-smooth" : ""} ${loading ? "is-loading" : ""}`}
        ref={drawing}
        style={{
          transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
        }}
        dangerouslySetInnerHTML={{ __html: svg }}
      />
      <div className="graph-legend" aria-hidden="true">
        {kinds.map((kind) => (
          <span key={kind}>
            <i className={`kind-dot kind-${kind}`} />
            {kindLabels[kind]}
          </span>
        ))}
      </div>
      <div className="canvas-tools" role="toolbar" aria-label="导图控制">
        <button title="缩小（-）" aria-label="缩小导图" onClick={() => zoom(1 / 1.2)}>
          <Icon name="minus" size={16} />
        </button>
        <span className="zoom-level" aria-live="polite">
          {Math.round(transform.scale * 100)}%
        </span>
        <button title="放大（+）" aria-label="放大导图" onClick={() => zoom(1.2)}>
          <Icon name="plus" size={16} />
        </button>
        <i />
        <button
          title="适应窗口（0）"
          aria-label="适应窗口"
          onClick={() => fit(true, true)}
        >
          <Icon name="fit" size={16} />
        </button>
        <button title="只保留主干" aria-label="折叠为主干" onClick={props.onCollapseAll}>
          <Icon name="collapse" size={16} />
        </button>
        <button
          title={`展开全部${collapsed.size ? `（${collapsed.size} 处折叠）` : ""}`}
          aria-label="展开全部节点"
          onClick={props.onExpandAll}
        >
          <Icon name="expand" size={16} />
        </button>
        <i />
        <button
          className={showRelations ? "is-on" : ""}
          title="显示节点间的支持、推导与限制关系"
          aria-label="显示全部关联"
          aria-pressed={showRelations}
          onClick={() => props.onShowRelations(!showRelations)}
        >
          <Icon name="link" size={16} />
        </button>
      </div>
      <div className="canvas-hint" aria-hidden="true">
        拖动或方向键平移 · Ctrl + 滚轮缩放 · <kbd>?</kbd> 快捷键
      </div>
    </div>
  );
}
