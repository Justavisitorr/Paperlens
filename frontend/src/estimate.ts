import type {
  CostRange,
  ProcessingEstimate,
  ProcessingProfileId,
  ProfileEstimate,
  Spread,
} from "./api";
import { formatMoneyRange, type MoneyDisplay } from "./currency";

const addSpread = (a: Spread, b: Spread): Spread => ({
  low: a.low + b.low,
  typical: a.typical + b.typical,
  high: a.high + b.high,
});

const addPair = (
  a?: [number, number],
  b?: [number, number],
): [number, number] | undefined => (a && b ? [a[0] + b[0], a[1] + b[1]] : a || b);

function addCost(a: CostRange, b: CostRange): CostRange {
  const knownA = a.status === "unknown" ? undefined : a;
  const knownB = b.status === "unknown" ? undefined : b;
  const off_peak = addPair(knownA?.off_peak, knownB?.off_peak);
  const peak = addPair(knownA?.peak, knownB?.peak);
  if (!off_peak && !peak) return { status: "unknown" };
  const complete =
    a.status === "range" &&
    b.status === "range" &&
    !!a.off_peak === !!b.off_peak &&
    !!a.peak === !!b.peak;
  if (complete) return { status: "range", off_peak, peak };
  return {
    status: "partial",
    off_peak,
    peak,
    unpriced_models: [
      ...new Set([
        ...(a.status === "partial" ? a.unpriced_models : []),
        ...(b.status === "partial" ? b.unpriced_models : []),
      ]),
    ],
  };
}

function addProfile(a: ProfileEstimate, b: ProfileEstimate): ProfileEstimate {
  return {
    ...a,
    samples: Math.min(a.samples, b.samples),
    same_profile: a.same_profile && b.same_profile,
    budget_limited: a.budget_limited || b.budget_limited,
    // Each paper gets its own request cap, so ceilings add up across papers.
    ceiling:
      a.ceiling && b.ceiling
        ? {
            requests: a.ceiling.requests + b.ceiling.requests,
            prompt_tokens: a.ceiling.prompt_tokens + b.ceiling.prompt_tokens,
            completion_tokens: a.ceiling.completion_tokens + b.ceiling.completion_tokens,
            cost: addCost(a.ceiling.cost, b.ceiling.cost),
          }
        : null,
    typical:
      a.typical && b.typical
        ? {
            requests: addSpread(a.typical.requests, b.typical.requests),
            prompt_tokens: addSpread(a.typical.prompt_tokens, b.typical.prompt_tokens),
            completion_tokens: addSpread(
              a.typical.completion_tokens,
              b.typical.completion_tokens,
            ),
            seconds: addSpread(a.typical.seconds, b.typical.seconds),
            cost: {
              low: addCost(a.typical.cost.low, b.typical.cost.low),
              typical: addCost(a.typical.cost.typical, b.typical.cost.typical),
              high: addCost(a.typical.cost.high, b.typical.cost.high),
            },
          }
        : null,
  };
}

function scheduledSeconds(items: ProfileEstimate[], workers: number): Spread | null {
  if (items.some((item) => !item.typical)) return null;
  const makespan = (key: keyof Spread) => {
    const slots = Array.from({ length: Math.min(workers, items.length) }, () => 0);
    for (const item of items) {
      const slot = slots.indexOf(Math.min(...slots));
      slots[slot] += item.typical!.seconds[key];
    }
    return Math.max(...slots);
  };
  const typical = makespan("typical");
  return {
    low: Math.min(makespan("low"), typical),
    typical,
    high: Math.max(makespan("high"), typical),
  };
}

/** Usage adds up; elapsed time follows the server's simultaneous job limit. */
export function combineEstimates(list: ProcessingEstimate[]): ProcessingEstimate | null {
  if (!list.length) return null;
  const combined = list.reduce((sum, next) => ({
    ...sum,
    pages: sum.pages + next.pages,
    pages_assumed: sum.pages_assumed || next.pages_assumed,
    price_stale: sum.price_stale || next.price_stale,
    profiles: sum.profiles.map((item) => {
      const other = next.profiles.find((p) => p.profile === item.profile);
      return other ? addProfile(item, other) : { ...item, typical: null };
    }),
  }));
  const workers = Math.min(
    ...list.map((item) => Math.max(1, Math.floor(item.parallel_jobs || 1))),
  );
  return {
    ...combined,
    parallel_jobs: workers,
    profiles: combined.profiles.map((item) => {
      const parts = list.map((entry) =>
        entry.profiles.find((p) => p.profile === item.profile),
      );
      const seconds = parts.every((part): part is ProfileEstimate => !!part)
        ? scheduledSeconds(parts, workers)
        : null;
      return {
        ...item,
        typical: item.typical && seconds ? { ...item.typical, seconds } : null,
      };
    }),
  };
}

export function profileEstimate(
  estimate: ProcessingEstimate | null,
  profile: ProcessingProfileId,
): ProfileEstimate | undefined {
  return estimate?.profiles.find((item) => item.profile === profile);
}

export function formatUsd(value: number): string {
  if (value === 0) return "$0";
  if (value < 0.01) return "<$0.01";
  return `$${value < 10 ? value.toFixed(2) : Math.round(value).toLocaleString()}`;
}

/** "$0.05–0.07" for one period, or a single value when both ends round alike. */
export function formatUsdRange([low, high]: [number, number]): string {
  const a = formatUsd(low);
  const b = formatUsd(high);
  return a === b ? a : `${a}–${b.replace("$", "")}`;
}

/** The off-peak price is the cheaper reading; peak is shown beside it. */
export function costText(
  cost: CostRange | undefined,
  money?: MoneyDisplay,
): string | null {
  if (!cost || cost.status !== "range" || !cost.off_peak) return null;
  const format = (pair: [number, number]) =>
    money ? formatMoneyRange(pair, money) : formatUsdRange(pair);
  const parts = [`${format(cost.off_peak)}（闲时）`];
  if (cost.peak) parts.push(`${format(cost.peak)}（高峰）`);
  return parts.join(" · ");
}

/** Compact USD range across billing periods; a partial quote is never a total. */
export function estimateCostText(
  cost: CostRange | undefined,
  money?: MoneyDisplay,
): string {
  if (!cost || cost.status === "unknown") return "费用待确认";
  const pairs = [cost.off_peak, cost.peak].filter(
    (pair): pair is [number, number] => !!pair,
  );
  if (!pairs.length) return "费用待确认";
  const pair: [number, number] = [
    Math.min(...pairs.map((pair) => pair[0])),
    Math.max(...pairs.map((pair) => pair[1])),
  ];
  const range = money
    ? formatMoneyRange(pair, money)
    : formatUsdRange(pair).replaceAll("$", "US$");
  return cost.status === "partial" ? `已知部分 ${range}` : range;
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return "不到 1 分钟";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `约 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `约 ${hours} 小时 ${rest} 分钟` : `约 ${hours} 小时`;
}

/** Compact interval for uncertain estimates, without exposing calculation details. */
export function formatDurationRange(seconds: Spread): string {
  const low = formatDuration(seconds.low).replace(/^约 /, "");
  const high = formatDuration(seconds.high).replace(/^约 /, "");
  return low === high ? low : `${low}至${high}`;
}
