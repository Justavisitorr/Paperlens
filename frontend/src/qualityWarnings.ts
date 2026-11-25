export interface WarningGroup {
  title: string;
  summary: string;
  pages: number[];
  details: { text: string; pages: number[] }[];
}
const categories: [RegExp, string, string][] = [
  [/未识别字形|乱码|缺字|占位标注/, "文字识别", "部分字形需核对原页"],
  [/遗漏的原生文字|原生文字.*补充/, "文字补全", "已补回遗漏的文字"],
  [/图片结果|图表.*区域|图表.*读取|图像|图片/, "图表读取", "部分图表需核对原页"],
  [/数学|公式|LaTeX|MathML/i, "公式识别", "部分公式需核对原页"],
  [/自动笔记/, "笔记生成", "部分笔记未生成，可重试"],
  [/阅读投影|正文重排|版面|跨栏/, "正文排版", "部分页面仍使用原始排版"],
  [/引句|引文|证据|定量结论/, "引文核对", "部分结论依据需核对"],
];
export function pageRanges(pages: number[]): string {
  const sorted = [...new Set(pages)].sort((a, b) => a - b);
  const ranges: string[] = [];
  for (let i = 0; i < sorted.length; i++) {
    const start = sorted[i];
    let end = start;
    while (sorted[i + 1] === end + 1) end = sorted[++i];
    ranges.push(start === end ? String(start) : `${start}–${end}`);
  }
  return ranges.length ? `第 ${ranges.join("、")} 页` : "";
}
export function groupWarnings(warnings: string[]): WarningGroup[] {
  const groups = new Map<string, WarningGroup>();
  for (const text of new Set(warnings)) {
    const category = categories.find(([pattern]) => pattern.test(text));
    const title = category?.[1] || "其他提示";
    const pages = [...text.matchAll(/第\s*(\d+)\s*页/g)].map((m) => Number(m[1]));
    const detail = text.replace(/^第\s*\d+\s*页/, "本页");
    const group = groups.get(title) || {
      title,
      summary: category?.[2] || "展开查看具体事项",
      pages: [],
      details: [],
    };
    group.pages = [...new Set([...group.pages, ...pages])].sort((a, b) => a - b);
    const existing = group.details.find((d) => d.text === detail);
    if (existing)
      existing.pages = [...new Set([...existing.pages, ...pages])].sort((a, b) => a - b);
    else group.details.push({ text: detail, pages });
    groups.set(title, group);
  }
  return [...groups.values()];
}
