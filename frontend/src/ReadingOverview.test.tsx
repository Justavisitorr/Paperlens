import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { ReadingOverview } from "./ReadingOverview";
import { ResultConditions } from "./ResultConditions";
import { reviewReport } from "./readingReview.fixtures";

it("reuses existing report content with argument, text and review entrances", () => {
  const html = renderToStaticMarkup(
    <ReadingOverview
      report={reviewReport()}
      onMap={() => {}}
      onText={() => {}}
      onNavigate={() => {}}
    />,
  );
  for (const text of [
    "Existing summary",
    "Existing question",
    "Existing contribution",
    "Existing limit",
    "Read methods",
    "进入论证图",
    "开始阅读正文",
  ])
    expect(html).toContain(text);
  expect(html).not.toContain("核查清单");
  expect(html).toContain('role="tablist"');
});
it("shows extracted experiment conditions, their citation and careful missing wording", () => {
  const report = reviewReport();
  const html = renderToStaticMarkup(
    <ResultConditions result={report.quantitative_results![0]} report={report} />,
  );
  expect(html).toContain("held out");
  expect(html).toContain("条件出处 P2");
  expect(html).toContain("Held-out test split");
  expect(html).toContain("样本量、硬件");
  expect(html).toContain("这不代表论文未说明");
});
