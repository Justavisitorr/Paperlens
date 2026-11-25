import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

export const CURRENCIES = {
  CNY: "人民币",
  USD: "美元",
  EUR: "欧元",
  GBP: "英镑",
  JPY: "日元",
  HKD: "港币",
  AUD: "澳大利亚元",
  CAD: "加拿大元",
  CHF: "瑞士法郎",
  SGD: "新加坡元",
  KRW: "韩元",
} as const;
export type CurrencyCode = keyof typeof CURRENCIES;
export const normalizeCurrency = (value: unknown): CurrencyCode =>
  typeof value === "string" && Object.hasOwn(CURRENCIES, value)
    ? (value as CurrencyCode)
    : "CNY";

export interface ExchangeRates {
  base: "USD";
  rates: Record<CurrencyCode, number>;
  date: string;
  source: string;
  source_url: string;
  checked_at: string | null;
  stale: boolean;
  snapshot: boolean;
}
export interface MoneyDisplay {
  currency: CurrencyCode;
  requested: CurrencyCode;
  rate: number;
  date?: string;
  stale?: boolean;
  unavailable?: boolean;
}
export const USD_DISPLAY: MoneyDisplay = { currency: "USD", requested: "USD", rate: 1 };
const MoneyContext = createContext<MoneyDisplay>(USD_DISPLAY);
const CACHE_KEY = "paperlens.exchangeRates";

export function validExchangeRates(value: unknown): value is ExchangeRates {
  const data = value as ExchangeRates | null;
  const date = new Date(`${data?.date}T00:00:00Z`);
  return (
    !!data &&
    data.base === "USD" &&
    /^\d{4}-\d{2}-\d{2}$/.test(data.date) &&
    Number.isFinite(date.getTime()) &&
    date.toISOString().slice(0, 10) === data.date &&
    data.rates?.USD === 1 &&
    Object.keys(CURRENCIES).every((key) => {
      const rate = data.rates[key as CurrencyCode];
      return typeof rate === "number" && Number.isFinite(rate) && rate > 0 && rate < 1e9;
    })
  );
}
function cachedRates(): ExchangeRates | null {
  try {
    const data: unknown = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
    return validExchangeRates(data) ? { ...data, stale: true } : null;
  } catch {
    return null;
  }
}

export function moneyDisplay(
  currency: CurrencyCode,
  rates: ExchangeRates | null,
): MoneyDisplay {
  if (currency === "USD") return USD_DISPLAY;
  return rates
    ? {
        currency,
        requested: currency,
        rate: rates.rates[currency],
        date: rates.date,
        stale: rates.stale || rates.snapshot,
      }
    : { ...USD_DISPLAY, requested: currency, unavailable: true };
}

export function CurrencyProvider({
  currency,
  enabled,
  children,
}: {
  currency: CurrencyCode;
  enabled: boolean;
  children: ReactNode;
}) {
  const [rates, setRates] = useState<ExchangeRates | null>(cachedRates);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    fetch("/api/exchange-rates", { signal: controller.signal })
      .then((response) => (response.ok ? response.json() : Promise.reject()))
      .then((data: unknown) => {
        if (controller.signal.aborted || !validExchangeRates(data)) return;
        setRates(data);
        try {
          localStorage.setItem(CACHE_KEY, JSON.stringify(data));
        } catch {
          /* display still works */
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [enabled]);
  return <MoneyContext value={moneyDisplay(currency, rates)}>{children}</MoneyContext>;
}
export const useMoney = () => useContext(MoneyContext);

const SYMBOLS: Record<CurrencyCode, string> = {
  CNY: "¥",
  USD: "US$",
  EUR: "€",
  GBP: "£",
  JPY: "JP¥",
  HKD: "HK$",
  AUD: "A$",
  CAD: "CA$",
  CHF: "CHF ",
  SGD: "S$",
  KRW: "₩",
};
/** Values stay in USD at rest. Conversion happens once, at the display boundary. */
export function formatMoney(usd: number, display: MoneyDisplay = USD_DISPLAY): string {
  const value = usd * display.rate;
  const symbol = SYMBOLS[display.currency];
  const digits = display.currency === "JPY" || display.currency === "KRW" ? 0 : 2;
  const minimum = 10 ** -digits;
  if (value === 0) return `${symbol}0`;
  if (value < minimum) return `<${symbol}${minimum.toFixed(digits)}`;
  return (
    symbol +
    value.toLocaleString("zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    })
  );
}
export function formatMoneyRange(
  pair: [number, number],
  display: MoneyDisplay = USD_DISPLAY,
): string {
  const a = formatMoney(pair[0], display),
    b = formatMoney(pair[1], display);
  return a === b ? a : `${a}–${b.replace(SYMBOLS[display.currency], "")}`;
}
export function exchangeNote(display: MoneyDisplay): string | null {
  if (display.unavailable)
    return `${CURRENCIES[display.requested]}汇率暂不可用，暂显示美元。`;
  if (display.currency === "USD") return null;
  return `按 ${display.date} 参考汇率换算${display.stale ? "（缓存参考，尚未刷新）" : ""}。`;
}
