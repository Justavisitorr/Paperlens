import { expect, it } from "vitest";
import { groupWarnings, pageRanges } from "./qualityWarnings";

it("deduplicates page warnings and retains distinct reasons", () => {
  const warnings = [1, 5, 6, 17, 18, 19, 20, 20].map(
    (p) => `第 ${p} 页存在未识别字形，已用占位标注；原文和页面位置保留。`,
  );
  warnings.push(
    "第 2 页图片结果校验失败，不能用作定量结论。",
    "第 8 页图片结果无法读取。",
    "未归类的具体原因123",
  );
  const groups = groupWarnings(warnings);
  expect(groups).toHaveLength(3);
  expect(groups[0].details).toHaveLength(1);
  expect(pageRanges(groups[0].pages)).toBe("第 1、5–6、17–20 页");
  expect(groups[1].details).toHaveLength(2);
  expect(groups[2].details[0].text).toBe("未归类的具体原因123");
  expect(pageRanges([])).toBe("");
});
