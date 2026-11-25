import { markdownListItem, markdownQuote } from "./markdownExport";
import { evidencePageLabel } from "./evidenceSource";
import {
  informationParts,
  readingDocument,
  readingText,
  referenceNumber,
} from "./reading";
import type { DashboardReport, ReadingItem } from "./types";

/** A file name stem from a paper title. */
export const fileStem = (title: string) =>
  title.slice(0, 55).replace(/[\\/:*?"<>|]/g, "_") || "paperlens";

export function downloadFile(content: string | Blob, name: string, type: string): void {
  const url = URL.createObjectURL(
    typeof content === "string" ? new Blob([content], { type }) : content,
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  // Firefox ignores clicks on links that are not in the document.
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Export the same original reading text as the reader, including saved corrections. */
export function toPlainText(report: DashboardReport): string {
  const reading = readingDocument(report);
  const blocks = new Map(report.document.blocks.map((block) => [block.id, block]));
  const lines = [report.analysis.title || report.document.title, ""];
  const add = (item: ReadingItem, section = "") => {
    let text = readingText(item, blocks);
    if (!text.trim()) return;
    const number = item.kind === "reference" ? referenceNumber(item) : "";
    if (number && !text.trimStart().startsWith(`[${number}]`))
      text = `[${number}] ${text}`;
    const pages = item.source_pages?.length
      ? item.source_pages
      : [
          ...new Set(
            item.parts.map((part) => blocks.get(part.block_id)?.page || item.page),
          ),
        ];
    lines.push(
      `[PDF 第 ${(pages.length ? pages : [item.page]).join("、")} 页] ${section}`.trim(),
      text,
      "",
    );
  };
  let section = "";
  for (const item of reading.items) {
    if (item.kind === "heading") section = readingText(item, blocks) || item.label;
    add(item, section);
  }
  if (reading.references.length) {
    lines.push("参考文献", "");
    for (const item of reading.references) add(item);
  }
  const parts = informationParts(reading, blocks);
  const keys = new Set([
    ...Object.keys(parts),
    ...Object.keys(reading.information_items || {}),
  ]);
  const labels: Record<string, string> = {
    publication: "出版信息",
    citation: "引用本文",
    authors: "作者信息",
    declarations: "基金声明",
    footnotes: "原文脚注",
    titles: "其他标题",
  };
  for (const key of keys) {
    const label = labels[key] || key;
    const items = reading.information_items?.[key];
    if (items) {
      for (const item of items) add(item, label);
    } else {
      for (const part of parts[key] || []) {
        const block = blocks.get(part.block_id);
        const text = block?.text.slice(part.start, part.end);
        if (text?.trim()) lines.push(`[PDF 第 ${block!.page} 页] ${label}`, text, "");
      }
    }
  }
  return lines.join("\n");
}

export function toMarkdown(report: DashboardReport): string {
  const lines = [
    `# ${report.analysis.title}`,
    "",
    report.analysis.summary,
    "",
    `**研究问题：** ${report.analysis.research_question}`,
    "",
  ];
  for (const node of report.analysis.nodes) {
    lines.push(
      `## ${node.title}`,
      "",
      node.summary,
      "",
      ...(node.explanation ? [`**通俗解读：** ${node.explanation}`, ""] : []),
    );
    lines.push(
      `来源：${node.origin === "author" ? "作者论述" : node.origin === "inference" ? "系统推断" : "阅读辅助"}；支持状态：${node.support}`,
      "",
    );
    for (const id of node.evidence_ids) {
      const evidence = report.evidence.find((e) => e.id === id);
      if (evidence)
        lines.push(
          markdownQuote(evidence.quote),
          markdownQuote(
            `— ${evidencePageLabel(evidence)}${evidence.section ? `，${evidence.section}` : ""}`,
          ),
          "",
        );
    }
  }
  if (report.analysis.glossary.length) {
    lines.push("## 术语解释", "");
    for (const term of report.analysis.glossary)
      lines.push(markdownListItem(`**${term.term}**：${term.explanation}`));
  }
  const warnings = [
    ...new Set([
      ...report.quality.warnings,
      ...report.analysis.warnings,
      ...report.document.warnings,
    ]),
  ];
  if (warnings.length)
    lines.push("", "## 处理说明", "", ...warnings.map((w) => markdownListItem(w)));
  return lines.join("\n");
}

// SVG presentation inherited from the reader must survive opening the file without
// the app stylesheet (including text alignment, hidden decorations and dashed edges).
const svgStyleProperties = [
  "fill",
  "fill-opacity",
  "fill-rule",
  "stroke",
  "stroke-width",
  "stroke-opacity",
  "stroke-dasharray",
  "stroke-dashoffset",
  "stroke-linecap",
  "stroke-linejoin",
  "stroke-miterlimit",
  "font-family",
  "font-size",
  "font-weight",
  "font-style",
  "font-variant",
  "letter-spacing",
  "word-spacing",
  "text-anchor",
  "dominant-baseline",
  "alignment-baseline",
  "baseline-shift",
  "text-decoration",
  "white-space",
  "color",
  "opacity",
  "display",
  "visibility",
  "paint-order",
];

/** Freeze a clean copy in the reader's styling context, without changing the live map. */
export function standaloneSvg(source: SVGSVGElement): {
  text: string;
  width: number;
  height: number;
} {
  const clone = source.cloneNode(true) as SVGSVGElement;
  const elements = [clone, ...clone.querySelectorAll<SVGElement>("*")];
  for (const el of elements) {
    el.classList.remove(
      "has-selection",
      "is-selected",
      "is-near",
      "is-match",
      "is-new",
      "is-focus",
      "draw-in",
    );
    el.style.setProperty("animation", "none", "important");
    el.style.setProperty("transition", "none", "important");
    el.removeAttribute("tabindex");
    el.removeAttribute("aria-pressed");
    if (el.getAttribute("role") === "button") el.removeAttribute("role");
  }
  // Opacity on the wrapper hides the temporary copy without inheriting hidden
  // visibility into its text. Absolute positioning leaves the live layout alone.
  const host = document.createElement("div");
  host.style.cssText = "position:absolute;left:0;top:0;opacity:0;pointer-events:none";
  host.setAttribute("aria-hidden", "true");
  host.append(clone);
  (source.parentElement || document.body).append(host);
  try {
    for (const el of elements) {
      if (!(el instanceof SVGElement)) continue;
      const computed = getComputedStyle(el);
      for (const property of svgStyleProperties)
        el.style.setProperty(property, computed.getPropertyValue(property));
      // The colour-vision palette widens these strips through CSS geometry.
      if (el.matches("rect.pl-kind"))
        el.setAttribute("width", String(parseFloat(computed.width)));
      // Also support SVG readers that use presentation attributes for text layout.
      if (el.matches("text, tspan")) {
        el.setAttribute("text-anchor", computed.textAnchor);
        el.setAttribute("dominant-baseline", computed.dominantBaseline);
      }
    }
    clone.querySelectorAll("style").forEach((style) => style.remove());

    // Mermaid's viewBox predates our relation overlays. Measure in SVG coordinates
    // so curves and labels outside it are included regardless of pan/zoom on screen.
    const bounds = clone.getBBox();
    const viewBox = source.viewBox.baseVal;
    const padding = 12;
    const x = Math.floor(Math.min(viewBox.x, bounds.x - padding));
    const y = Math.floor(Math.min(viewBox.y, bounds.y - padding));
    const width = Math.max(
      1,
      Math.ceil(
        Math.max(viewBox.x + viewBox.width, bounds.x + bounds.width + padding) - x,
      ),
    );
    const height = Math.max(
      1,
      Math.ceil(
        Math.max(viewBox.y + viewBox.height, bounds.y + bounds.height + padding) - y,
      ),
    );
    const viewport = source.closest(".graph-viewport");
    const background = viewport
      ? getComputedStyle(viewport).backgroundColor
      : getComputedStyle(source).getPropertyValue("--bg").trim() || "#ffffff";
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("viewBox", `${x} ${y} ${width} ${height}`);
    clone.setAttribute("width", String(width));
    clone.setAttribute("height", String(height));
    clone.removeAttribute("aria-label");
    clone.style.maxWidth = "none";
    clone.style.background = background;
    // A painted background also works in image decoders that ignore SVG CSS backgrounds.
    const backdrop = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const [key, value] of Object.entries({
      x,
      y,
      width,
      height,
      fill: background,
      stroke: "none",
    }))
      backdrop.setAttribute(key, String(value));
    clone.prepend(backdrop);
    return { text: new XMLSerializer().serializeToString(clone), width, height };
  } finally {
    host.remove();
  }
}

export async function exportPng(svg: SVGSVGElement, name: string): Promise<void> {
  const { text, width, height } = standaloneSvg(svg);
  const url = URL.createObjectURL(
    new Blob([text], { type: "image/svg+xml;charset=utf-8" }),
  );
  try {
    if (svg.querySelector("foreignObject"))
      throw new Error("导图含 HTML 标签，请重新生成使用纯 SVG 标签的报告后导出 PNG。");
    const img = new Image();
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error("导图图片解码超时，请尝试导出 SVG。")),
        10000,
      );
      img.onload = () => {
        clearTimeout(timer);
        resolve();
      };
      img.onerror = () => {
        clearTimeout(timer);
        reject(new Error("导图图片解码失败，请尝试导出 SVG。"));
      };
      img.src = url;
    });
    const scale = Math.min(
      2,
      10000 / Math.max(width, height),
      Math.sqrt(24_000_000 / (width * height)),
    );
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.ceil(width * scale));
    canvas.height = Math.max(1, Math.ceil(height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("浏览器不支持图片导出");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob(
        (b) => (b ? resolve(b) : reject(new Error("图片编码失败"))),
        "image/png",
      ),
    );
    downloadFile(blob, name, "image/png");
  } finally {
    URL.revokeObjectURL(url);
  }
}
