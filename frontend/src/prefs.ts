import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { normalizeCurrency, type CurrencyCode } from "./currency";

/** Local preferences; the standalone account adapter additionally synchronizes them. */
export interface Prefs {
  currency: CurrencyCode;
  theme: "light" | "dark" | "system";
  /** Each mode keeps its own palette, so switching modes never mixes them. */
  lightPalette: LightPalette;
  darkPalette: DarkPalette;
  textSize: "standard" | "large";
  /** Base UI text size in px; prose uses independent reading preferences. */
  uiFontSize: number;
  motion: "full" | "reduced";
  /** "cvd" swaps to a palette that stays distinct for red–green colour blindness. */
  colorVision: "standard" | "cvd";
  showRelations: boolean;
  /** Retained for compatibility with older saved preferences; all notes now offer AI replies. */
  noteAIReview: boolean;
  /** Wide screens only; narrow screens always start with the rail. */
  sidebarCollapsed: boolean;
  /** How much of a map is unfolded when it opens. */
  mapDepth: "auto" | "first" | "all";
  /** Explicit full-read action; ordinary paper entry opens its overview. */
  fullReadMode: "map" | "text";
  /** Bumped when the default palette changes, so older saves move to it once. */
  paletteDefaults: number;
}
export type LightPalette = "default" | "paper" | "celadon" | "clay" | "mono-light";
export type DarkPalette = "default" | "lamp" | "kiln" | "midnight" | "mono-dark";

/** Name, one-line feel and preview swatches [page, side, text, accent]. */
export const PALETTES: {
  light: [LightPalette, string, string, string[]][];
  dark: [DarkPalette, string, string, string[]][];
} = {
  light: [
    [
      "clay",
      "陶土",
      "象牙纸色配陶土橙，衬线标题，像一本排好版的期刊",
      ["#faf9f5", "#f3f1ea", "#191915", "#a24a2e"],
    ],
    [
      "default",
      "清爽",
      "中性浅灰配蓝色强调，正文与界面保持一致",
      ["#fbfbfa", "#f7f7f5", "#1f2328", "#2f6bd8"],
    ],
    [
      "paper",
      "纸本",
      "米黄纸色配青色强调，像在读一本书",
      ["#f6f3ec", "#f1ede4", "#23211d", "#2d6173"],
    ],
    [
      "celadon",
      "青瓷",
      "淡灰绿底色，长时间阅读更安静",
      ["#f3f6f4", "#edf2ef", "#1d2622", "#2f7a63"],
    ],
    [
      "mono-light",
      "素白",
      "只有黑白灰，最少干扰",
      ["#fafafa", "#f5f5f5", "#1a1a1a", "#2b2b2b"],
    ],
  ],
  dark: [
    [
      "default",
      "石墨",
      "中性深灰配蓝色强调",
      ["#161719", "#191a1c", "#e7e7e4", "#6c9ef3"],
    ],
    [
      "lamp",
      "暖夜",
      "暖黑底色，像台灯下的纸页",
      ["#1a1917", "#1d1c19", "#e6e1d6", "#7fb2c2"],
    ],
    [
      "kiln",
      "窑火",
      "暖炭底色配余烬橙，陶土的夜间版",
      ["#1c1b19", "#1f1e1b", "#f0ece2", "#e08a66"],
    ],
    [
      "midnight",
      "深海",
      "深蓝紫底色配冷色强调",
      ["#11131f", "#141725", "#e3e5f2", "#9c9cf5"],
    ],
    [
      "mono-dark",
      "纯黑",
      "黑白灰，适合 OLED 屏幕",
      ["#0e0e0e", "#121212", "#e8e8e8", "#e0e0e0"],
    ],
  ],
};

const KEY = "paperlens.prefs";
const defaults: Prefs = {
  currency: "CNY",
  theme: "light",
  lightPalette: "clay",
  darkPalette: "default",
  textSize: "standard",
  uiFontSize: 13,
  motion: "full",
  colorVision: "standard",
  showRelations: false,
  noteAIReview: false,
  sidebarCollapsed: false,
  mapDepth: "auto",
  fullReadMode: "text",
  paletteDefaults: 1,
};

export function interfaceFontSize(value: unknown, fallback = 13) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.min(24, Math.max(12, Math.round(value)))
    : fallback;
}

export function normalizePrefs(value: Partial<Prefs> | null): Prefs {
  // Saves from before 陶土 became the default still name the old one explicitly.
  const migrate = !value?.paletteDefaults && value?.lightPalette === "default";
  return {
    ...defaults,
    ...value,
    ...(migrate && { lightPalette: "clay" as const }),
    paletteDefaults: defaults.paletteDefaults,
    currency: normalizeCurrency(value?.currency),
    fullReadMode: value?.fullReadMode === "map" ? "map" : "text",
    uiFontSize: interfaceFontSize(
      value?.uiFontSize,
      value?.textSize === "large" ? 14 : 13,
    ),
  };
}

function load(): Prefs {
  try {
    return normalizePrefs(JSON.parse(localStorage.getItem(KEY) || "{}"));
  } catch {
    return defaults;
  }
}

export function usePrefs(): [Prefs, (patch: Partial<Prefs>) => void] {
  const [prefs, setPrefs] = useState(load);
  const update = useCallback((patch: Partial<Prefs>) => {
    setPrefs((old) => {
      const next = normalizePrefs({ ...old, ...patch });
      try {
        localStorage.setItem(KEY, JSON.stringify(next));
      } catch {
        /* preferences simply won't persist */
      }
      return next;
    });
  }, []);
  const systemDark = useMedia("(prefers-color-scheme: dark)");
  const dark = prefs.theme === "dark" || (prefs.theme === "system" && systemDark);
  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = dark ? "dark" : "light";
    root.dataset.palette = dark ? prefs.darkPalette : prefs.lightPalette;
    root.dataset.textSize = prefs.textSize;
    root.style.setProperty("--ui-font-size", `${prefs.uiFontSize}px`);
    root.dataset.motion = prefs.motion;
    root.dataset.vision = prefs.colorVision;
  }, [
    dark,
    prefs.lightPalette,
    prefs.darkPalette,
    prefs.textSize,
    prefs.uiFontSize,
    prefs.motion,
    prefs.colorVision,
  ]);
  return [prefs, update];
}

export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const media = matchMedia(query);
      media.addEventListener("change", notify);
      return () => media.removeEventListener("change", notify);
    },
    () => matchMedia(query).matches,
  );
}

export const reducedMotion = () =>
  document.documentElement.dataset.motion === "reduced" ||
  matchMedia("(prefers-reduced-motion: reduce)").matches;
