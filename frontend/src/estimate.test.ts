import { describe, expect, it } from "vitest";
import type { ProcessingEstimate, ProfileEstimate } from "./api";
import {
  combineEstimates,
  costText,
  estimateCostText,
  formatDuration,
  formatDurationRange,
  formatUsd,
  formatUsdRange,
} from "./estimate";

const spread = (n: number) => ({ low: n, typical: n * 2, high: n * 3 });

function profile(cost: ProfileEstimate["ceiling"], priced = true): ProfileEstimate {
  const range = priced
    ? ({ status: "range", off_peak: [0.01, 0.02], peak: [0.02, 0.04] } as const)
    : ({ status: "unknown" } as const);
  return {
    profile: "standard",
    label: "标准",
    samples: 3,
    same_profile: true,
    ceiling: cost,
    typical: {
      requests: spread(2),
      prompt_tokens: spread(1000),
      completion_tokens: spread(500),
      seconds: spread(60),
      cost: { low: range, typical: range, high: range },
    },
  } as ProfileEstimate;
}

function estimate(
  pages: number,
  item: ProfileEstimate,
  assumed = false,
): ProcessingEstimate {
  return {
    pages,
    pages_assumed: assumed,
    model: "m",
    history_runs: 3,
    chars_per_token: 2,
    price_checked_at: null,
    price_stale: false,
    profiles: [item],
  } as ProcessingEstimate;
}

describe("combineEstimates", () => {
  it("adds pages, usage, ceilings and prices across files", () => {
    const ceiling = {
      requests: 10,
      prompt_tokens: 100,
      completion_tokens: 50,
      cost: { status: "unknown" },
    } as const;
    const sum = combineEstimates([
      estimate(4, profile(ceiling)),
      estimate(6, profile(ceiling), true),
    ])!;
    const item = sum.profiles[0];
    expect(sum.pages).toBe(10);
    expect(sum.pages_assumed).toBe(true);
    expect(item.ceiling?.requests).toBe(20);
    expect(item.ceiling?.cost.status).toBe("unknown");
    expect(item.typical?.seconds).toEqual({ low: 120, typical: 240, high: 360 });
    expect(item.typical?.cost.typical).toMatchObject({ off_peak: [0.02, 0.04] });
  });

  it("keeps the known subtotal when another file has no price", () => {
    const sum = combineEstimates([
      estimate(1, profile(null)),
      estimate(1, profile(null, false)),
    ])!;
    expect(sum.profiles[0].typical?.cost.typical).toMatchObject({
      status: "partial",
      off_peak: [0.01, 0.02],
      peak: [0.02, 0.04],
    });
    expect(sum.profiles[0].ceiling).toBeNull();
  });

  it("adds partial prices and retains unknown models and completion risks", () => {
    const a = profile(null),
      b = profile(null);
    a.typical!.cost.typical = {
      status: "partial",
      off_peak: [0.1, 0.2],
      unpriced_models: ["custom", "vision"],
    };
    b.typical!.cost.typical = {
      status: "partial",
      off_peak: [0.3, 0.4],
      unpriced_models: ["custom"],
    };
    b.budget_limited = true;
    const result = combineEstimates([estimate(1, a), estimate(1, b)])!.profiles[0];
    expect(result.typical!.cost.typical).toEqual({
      status: "partial",
      off_peak: [0.4, 0.6000000000000001],
      peak: undefined,
      unpriced_models: ["custom", "vision"],
    });
    expect(result.budget_limited).toBe(true);
    expect(estimateCostText(result.typical!.cost.typical)).toBe("已知部分 US$0.40–0.60");
  });

  it("returns null for no files", () => {
    expect(combineEstimates([])).toBeNull();
  });

  it("estimates batch completion with the server's parallel job slots", () => {
    const files = [5, 4, 2].map((n) => {
      const item = estimate(n, profile(null));
      item.parallel_jobs = 2;
      item.profiles[0].typical!.seconds = spread(n * 60);
      return item;
    });
    const sum = combineEstimates(files)!;
    expect(sum.parallel_jobs).toBe(2);
    expect(sum.profiles[0].typical?.seconds).toEqual({
      low: 360,
      typical: 720,
      high: 1080,
    });
    expect(sum.profiles[0].typical?.requests.typical).toBe(12);
  });

  it("does not reuse one file's time for an unavailable profile in another", () => {
    const sum = combineEstimates([
      estimate(1, profile(null)),
      { ...estimate(2, profile(null)), profiles: [] },
    ])!;
    expect(sum.profiles[0].typical).toBeNull();
  });
});

describe("formatters", () => {
  it("formats money", () => {
    expect(formatUsd(0)).toBe("$0");
    expect(formatUsd(0.004)).toBe("<$0.01");
    expect(formatUsd(3.456)).toBe("$3.46");
    expect(formatUsdRange([0.05, 0.066])).toBe("$0.05–0.07");
    expect(formatUsdRange([0.051, 0.052])).toBe("$0.05");
    expect(costText({ status: "range", off_peak: [0.1, 0.2], peak: [0.2, 0.4] })).toBe(
      "$0.10–0.20（闲时） · $0.20–0.40（高峰）",
    );
    expect(costText({ status: "unknown" })).toBeNull();
    expect(estimateCostText({ status: "unknown" })).toBe("费用待确认");
    expect(
      estimateCostText({ status: "range", off_peak: [0.1, 0.2], peak: [0.2, 0.4] }),
    ).toBe("US$0.10–0.40");
    expect(
      costText({ status: "partial", off_peak: [0.1, 0.2], unpriced_models: ["custom"] }),
    ).toBeNull();
  });

  it("formats time", () => {
    expect(formatDuration(30)).toBe("不到 1 分钟");
    expect(formatDuration(300)).toBe("约 5 分钟");
    expect(formatDuration(3900)).toBe("约 1 小时 5 分钟");
    expect(formatDuration(7200)).toBe("约 2 小时");
    expect(formatDurationRange({ low: 60, typical: 120, high: 300 })).toBe(
      "1 分钟至5 分钟",
    );
    expect(formatDurationRange({ low: 30, typical: 35, high: 40 })).toBe("不到 1 分钟");
  });
});
