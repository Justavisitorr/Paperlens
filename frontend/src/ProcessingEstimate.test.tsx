import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
import type { ProcessingEstimate } from "./api";
import { ProcessingEstimateCard, tierEstimateLine } from "./ProcessingEstimate";
import { ProcessingTierPicker } from "./ProcessingTierPicker";

const spread = (n: number) => ({ low: n, typical: n * 2, high: n * 4 });
const estimate: ProcessingEstimate = {
  pages: 8,
  pages_assumed: false,
  model: "new-model",
  history_runs: 0,
  chars_per_token: 2,
  price_checked_at: null,
  price_stale: false,
  profiles: ["economy", "standard", "detailed"].map((id, index) => ({
    profile: id as ProcessingEstimate["profiles"][number]["profile"],
    label: ["经济", "标准", "精细"][index],
    samples: 0,
    same_profile: false,
    ceiling: null,
    typical: {
      requests: spread(1),
      prompt_tokens: spread(1000),
      completion_tokens: spread(500),
      seconds: spread(60 * (index + 1)),
      cost: {
        low: { status: "unknown" },
        typical: { status: "unknown" },
        high: { status: "unknown" },
      },
    },
  })),
};

describe("processing time estimates", () => {
  it("shows time and an explicit unknown price without calculation details", () => {
    const html = renderToStaticMarkup(
      <ProcessingEstimateCard
        estimate={estimate}
        item={estimate.profiles[1]}
        loading={false}
        files={1}
        queued={2}
      />,
    );
    expect(html).toContain("预计时间与费用");
    expect(html).toContain("约 4 分钟");
    expect(html).toContain("2 分钟至8 分钟");
    expect(html).toContain("等待时间另计");
    expect(html).toContain("费用待确认");
    expect(html).toContain("预计费用（美元）");
    for (const text of ["token", "调用", "本机", "上限", "推算"])
      expect(html).not.toContain(text);
  });

  it("immediately selects each tier's own time", () => {
    for (const [index, item] of estimate.profiles.entries()) {
      const html = renderToStaticMarkup(
        <ProcessingTierPicker
          profile={item.profile}
          catalog={null}
          error=""
          disabled={false}
          compact
          estimate={estimate}
          onChange={() => {}}
        />,
      );
      expect(html).toContain(`约 ${(index + 1) * 2} 分钟</strong>`);
      expect(tierEstimateLine(item)).toBe(`约 ${(index + 1) * 2} 分钟 · 费用待确认`);
      expect(html).not.toContain("tokens");
      expect(html).not.toContain("价格未知");
      expect(html).not.toContain("× 0.5");
    }
  });

  it("keeps the previous quote visible and labels it during a refresh", () => {
    const html = renderToStaticMarkup(
      <ProcessingTierPicker
        profile="standard"
        catalog={null}
        error=""
        disabled={false}
        estimate={estimate}
        estimating
        onChange={() => {}}
      />,
    );
    expect(html).toContain("更新中 · 上次估算");
    expect(html.match(/class="tier-estimate"/g)).toHaveLength(3);
    expect(html).toContain('class="estimate-summary"');
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain("约 4 分钟");
  });

  it("keeps the same card structure while waiting for the first estimate", () => {
    const html = renderToStaticMarkup(
      <ProcessingEstimateCard
        estimate={null}
        item={undefined}
        loading
        files={1}
        queued={0}
      />,
    );
    expect(html).toContain("正在估算时间与费用…");
    expect(html).toContain("预计时间与费用");
    expect(html).toContain('class="estimate-summary"');
    expect(html.match(/<strong>—<\/strong>/g)).toHaveLength(2);
  });

  it("distinguishes an unsuccessful refresh from a current quote", () => {
    const html = renderToStaticMarkup(
      <ProcessingEstimateCard
        estimate={estimate}
        item={estimate.profiles[1]}
        loading={false}
        error="本次估算未更新，请稍后重试。"
        files={1}
        queued={0}
      />,
    );
    expect(html).toContain("更新失败 · 上次估算");
    expect(html).toContain("约 4 分钟");
    expect(html).toContain('aria-busy="false"');
    expect(html).not.toContain("按当前设置估算");
  });

  it("starts pricing explanations collapsed while keeping budget risks visible", () => {
    const item = { ...estimate.profiles[0], budget_limited: true };
    const html = renderToStaticMarkup(
      <ProcessingEstimateCard
        estimate={estimate}
        item={item}
        loading={false}
        files={1}
        queued={0}
      />,
    );
    expect(html).toContain('<details class="estimate-details">');
    expect(html).not.toMatch(/<details[^>]*\sopen/);
    expect(html.indexOf("当前设置可能不足以完成处理。")).toBeLessThan(
      html.indexOf('<details class="estimate-details">'),
    );
  });

  it("labels partial prices as a subtotal instead of a total", () => {
    const item = structuredClone(estimate.profiles[0]);
    item.typical!.cost.typical = {
      status: "partial",
      off_peak: [0.04, 0.08],
      peak: [0.08, 0.16],
      unpriced_models: ["custom-model"],
    };
    const html = renderToStaticMarkup(
      <ProcessingEstimateCard
        estimate={estimate}
        item={item}
        loading={false}
        files={1}
        queued={0}
      />,
    );
    expect(html).toContain("已知部分 US$0.04–0.16");
    expect(html).toContain("以上不含这些费用");
  });

  it("shows an independent acceleration switch on all three cards", () => {
    const html = renderToStaticMarkup(
      <ProcessingTierPicker
        profile="economy"
        catalog={null}
        error=""
        disabled={false}
        compact
        accelerated
        onChange={() => {}}
        onAcceleratedChange={() => {}}
      />,
    );
    expect(html.match(/class="tier-accelerate"/g)).toHaveLength(3);
    expect(html.match(/aria-pressed="true"/g)).toHaveLength(3);
    expect(html).toContain("点击右上角闪电切换速度，连点两下进入极速。");
    for (const label of ["经济", "标准", "精细"])
      expect(html).toContain(`aria-label="${label}档加速"`);
    expect(html).not.toMatch(
      /<button[^>]*role="radio"[^>]*>(?:(?!<\/button>)[\s\S])*<button/,
    );
  });

  it("warns when the selected tier's request budget clips the work", () => {
    const item = { ...estimate.profiles[0], budget_limited: true };
    const html = renderToStaticMarkup(
      <ProcessingEstimateCard
        estimate={estimate}
        item={item}
        loading={false}
        files={1}
        queued={0}
      />,
    );
    expect(html).toContain("当前设置可能不足以完成处理。");
  });
});
