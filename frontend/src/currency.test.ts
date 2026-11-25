import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
import type { ProcessingEstimate } from "./api";
import {
  CURRENCIES,
  CurrencyProvider,
  exchangeNote,
  formatMoney,
  formatMoneyRange,
  moneyDisplay,
  normalizeCurrency,
  USD_DISPLAY,
  useMoney,
  validExchangeRates,
  type CurrencyCode,
  type ExchangeRates,
} from "./currency";
import { costText, estimateCostText } from "./estimate";
import { normalizePrefs } from "./prefs";
import { ProcessingEstimateCard } from "./ProcessingEstimate";

const reference: ExchangeRates = {
  base: "USD",
  rates: {
    USD: 1,
    CNY: 7,
    EUR: 0.9,
    GBP: 0.75,
    JPY: 150,
    HKD: 7.8,
    AUD: 1.4,
    CAD: 1.4,
    CHF: 0.8,
    SGD: 1.3,
    KRW: 1300,
  },
  date: "2026-10-16",
  source: "Frankfurter / European Central Bank",
  source_url: "https://api.frankfurter.dev/v2/providers/ecb/rates",
  checked_at: "2026-10-19T03:00:00Z",
  stale: false,
  snapshot: false,
};

const spread = { low: 60, typical: 120, high: 240 };
const estimate: ProcessingEstimate = {
  pages: 8,
  pages_assumed: false,
  model: "example",
  history_runs: 0,
  chars_per_token: 2,
  price_checked_at: reference.checked_at,
  price_stale: false,
  profiles: [
    {
      profile: "standard",
      label: "标准",
      samples: 0,
      same_profile: false,
      ceiling: null,
      typical: {
        requests: spread,
        prompt_tokens: spread,
        completion_tokens: spread,
        seconds: spread,
        cost: {
          low: { status: "range", off_peak: [1, 2] },
          typical: { status: "range", off_peak: [1, 2] },
          high: { status: "range", off_peak: [1, 2] },
        },
      },
    },
  ],
};

let storage: Map<string, string>;
beforeEach(() => {
  storage = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
});

describe("display currency preferences", () => {
  it("defaults new and older preferences to CNY without changing other reading choices", () => {
    expect(normalizePrefs(null).currency).toBe("CNY");
    const migrated = normalizePrefs({
      theme: "dark",
      lightPalette: "default",
      textSize: "large",
    });
    expect(migrated.currency).toBe("CNY");
    expect(migrated.theme).toBe("dark");
    expect(migrated.lightPalette).toBe("clay");
    expect(migrated.uiFontSize).toBe(14);
    expect(normalizePrefs({ currency: "USD" }).currency).toBe("USD");
    expect(normalizePrefs({ currency: "EUR", theme: "dark" }).currency).toBe("EUR");
  });

  it("accepts only supported ISO codes and sanitizes unknown saved values", () => {
    for (const code of Object.keys(CURRENCIES))
      expect(normalizeCurrency(code)).toBe(code);
    for (const invalid of [undefined, null, 1, "cny", "DOGE", "toString", "__proto__"])
      expect(normalizeCurrency(invalid)).toBe("CNY");
    expect(normalizePrefs({ currency: "DOGE" as CurrencyCode }).currency).toBe("CNY");
  });
});

describe("USD display boundary", () => {
  it("converts original USD values once and keeps the stored price pairs unchanged", () => {
    const money = moneyDisplay("CNY", reference);
    const cost = { status: "range" as const, off_peak: [1, 2] as [number, number] };
    expect(formatMoney(1, money)).toBe("¥7.00");
    expect(formatMoneyRange(cost.off_peak, money)).toBe("¥7.00–14.00");
    expect(estimateCostText(cost, money)).toBe("¥7.00–14.00");
    expect(costText(cost, money)).toBe("¥7.00–14.00（闲时）");
    expect(cost.off_peak).toEqual([1, 2]);
    expect(formatMoney(1, moneyDisplay("USD", reference))).toBe("US$1.00");
    expect(moneyDisplay("USD", reference)).toBe(USD_DISPLAY);
  });

  it("keeps USD amounts and an explicit fallback message when a requested rate is unavailable", () => {
    const money = moneyDisplay("CNY", null);
    expect(money.currency).toBe("USD");
    expect(money.requested).toBe("CNY");
    expect(formatMoney(1, money)).toBe("US$1.00");
    expect(exchangeNote(money)).toContain("人民币汇率暂不可用，暂显示美元");
    expect(exchangeNote(moneyDisplay("USD", null))).toBeNull();
  });

  it("formats other currencies and sub-cent costs without rounding a nonzero cost to zero", () => {
    const euros = moneyDisplay("EUR", reference);
    expect(formatMoney(1, euros)).toBe("€0.90");
    expect(formatMoney(0.001, euros)).toBe("<€0.01");
    expect(formatMoney(0, euros)).toBe("€0");
    expect(formatMoneyRange([0.001, 0.002], euros)).toBe("<€0.01");
    expect(formatMoney(1, moneyDisplay("GBP", reference))).toBe("£0.75");
    expect(formatMoney(1, moneyDisplay("HKD", reference))).toBe("HK$7.80");
  });

  it("uses whole currency units and distinct symbols for JPY and KRW", () => {
    const yen = moneyDisplay("JPY", reference);
    expect(formatMoney(1, yen)).toBe("JP¥150");
    expect(formatMoney(0.001, yen)).toBe("<JP¥1");
    expect(formatMoneyRange([1, 2], yen)).toBe("JP¥150–300");
    expect(formatMoney(1, moneyDisplay("KRW", reference))).toBe("₩1,300");
    expect(formatMoney(0.0001, moneyDisplay("KRW", reference))).toBe("<₩1");
  });

  it("retains the reference date and distinguishes a cached snapshot from refreshed rates", () => {
    expect(exchangeNote(moneyDisplay("CNY", reference))).toContain("2026-10-16");
    expect(exchangeNote(moneyDisplay("CNY", reference))).not.toContain("尚未刷新");
    const saved = { ...reference, snapshot: true };
    expect(exchangeNote(moneyDisplay("CNY", saved))).toContain("缓存参考，尚未刷新");
  });
});

describe("reference rate validation", () => {
  it("rejects wrong bases, missing rates, invalid identity conversion and invalid numeric values", () => {
    expect(validExchangeRates(reference)).toBe(true);
    expect(validExchangeRates(null)).toBe(false);
    expect(validExchangeRates({ ...reference, base: "EUR" })).toBe(false);
    expect(validExchangeRates({ ...reference, rates: { USD: 1, CNY: 7 } })).toBe(false);
    expect(
      validExchangeRates({ ...reference, rates: { ...reference.rates, USD: 2 } }),
    ).toBe(false);
    for (const invalid of [0, -1, Infinity, NaN, "7", true, undefined, 1e9])
      expect(
        validExchangeRates({ ...reference, rates: { ...reference.rates, CNY: invalid } }),
      ).toBe(false);
  });

  it("requires an actual calendar date rather than a matching text pattern", () => {
    for (const date of ["2026-99-99", "2026-02-30", "2026-1-01", "invalid", null])
      expect(validExchangeRates({ ...reference, date })).toBe(false);
    expect(validExchangeRates({ ...reference, date: "2024-02-29" })).toBe(true);
  });
});

describe("cost cards share the same currency provider", () => {
  const renderCard = (currency: CurrencyCode, partial = false) => {
    const value = structuredClone(estimate);
    if (partial)
      value.profiles[0].typical!.cost.typical = {
        status: "partial",
        off_peak: [1, 2],
        unpriced_models: ["unknown-model"],
      };
    return renderToStaticMarkup(
      createElement(CurrencyProvider, {
        currency,
        enabled: false,
        children: createElement(ProcessingEstimateCard, {
          estimate: value,
          item: value.profiles[0],
          loading: false,
          files: 1,
          queued: 0,
        }),
      }),
    );
  };

  it("uses the default CNY preference and a validated cached rate in the displayed card", () => {
    storage.set("paperlens.exchangeRates", JSON.stringify(reference));
    const html = renderCard(normalizePrefs(null).currency);
    expect(html).toContain("预计费用（人民币）");
    expect(html).toContain("¥7.00–14.00");
    expect(html).toContain("2026-10-16");
    expect(html).toContain("缓存参考");
  });

  it("rejects a poisoned local cache and displays actual USD without changing its symbol", () => {
    storage.set(
      "paperlens.exchangeRates",
      JSON.stringify({ ...reference, rates: { ...reference.rates, CNY: -7 } }),
    );
    const html = renderCard("CNY");
    expect(html).toContain("预计费用（美元）");
    expect(html).toContain("US$1.00–2.00");
    expect(html).toContain("人民币汇率暂不可用，暂显示美元");
    expect(html).not.toContain("¥");
  });

  it("shows conversion provenance and the unknown-model boundary for partial costs too", () => {
    storage.set("paperlens.exchangeRates", JSON.stringify(reference));
    const html = renderCard("CNY", true);
    expect(html).toContain("已知部分 ¥7.00–14.00");
    expect(html).toContain("2026-10-16");
    expect(html).toContain("以上不含这些费用");
    storage.clear();
    const fallback = renderCard("CNY", true);
    expect(fallback).toContain("已知部分 US$1.00–2.00");
    expect(fallback).toContain("人民币汇率暂不可用，暂显示美元");
  });

  it("context consumers use the same selected currency as the card", () => {
    storage.set("paperlens.exchangeRates", JSON.stringify(reference));
    const Cost = () => createElement("span", {}, formatMoney(1, useMoney()));
    const html = renderToStaticMarkup(
      createElement(CurrencyProvider, {
        currency: "EUR",
        enabled: false,
        children: createElement(Cost),
      }),
    );
    expect(html).toBe("<span>€0.90</span>");
  });
});
