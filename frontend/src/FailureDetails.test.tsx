import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { FailureDetails } from "./FailureDetails";
import { QualityWarnings } from "./WarningGroups";
import type { StageFailure } from "./types";

const failure: StageFailure = {
  stage: "text_review",
  label: "正文审校",
  model: "gpt-6-astra",
  pages: [4, 1, 2, 3],
  batches: 2,
  items: 26,
  code: "provider_rejected",
  message: "需要 JSON <input>",
  guidance: "修正参数后继续，原样重试无效。",
  retryable: false,
  http_status: 400,
};

it("makes a failed stage expandable with its model, pages, cause and next action", () => {
  const html = renderToStaticMarkup(<FailureDetails failures={[failure]} />);
  expect(html).toContain("<summary>未完成步骤 · 正文审校</summary>");
  expect(html).toContain("gpt-6-astra · PDF 第 1–4 页 · 2 个批次 · 26 项");
  expect(html).toContain("HTTP 400");
  expect(html).toContain("需要 JSON &lt;input&gt;");
  expect(html).toContain("原样重试无效");
  expect(html).not.toContain("<details open");
});

it("shows structured failures even without warnings and suppresses only the redundant blanket warning", () => {
  const html = renderToStaticMarkup(
    <QualityWarnings
      warnings={[
        "部分模型请求失败，本次处理尚未完成；可主动重试，已完成批次将复用。",
        "第 8 页公式需要核对",
      ]}
      failures={[failure]}
    />,
  );
  expect(html).toContain("正文审校");
  expect(html).toContain("公式需要核对");
  expect(html).not.toContain("部分模型请求失败");
  const onlyFailure = renderToStaticMarkup(
    <QualityWarnings warnings={[]} failures={[failure]} />,
  );
  expect(onlyFailure).not.toContain("未报告异常");
  expect(onlyFailure).toContain("正文审校");
  expect(renderToStaticMarkup(<FailureDetails />)).toBe("");
});
