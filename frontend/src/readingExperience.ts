import { useLayoutEffect, useState, type RefObject } from "react";
import type { Block, Box, ReadingItem } from "./types";

export interface ReadingPreferences {
  size: number;
  measure: number;
  leading: number;
  font: "serif" | "sans";
}
export const defaultReadingPreferences: ReadingPreferences = {
  size: 18,
  measure: 35,
  leading: 1.9,
  font: "serif",
};
/** Keep the saved preference; only the displayed width adapts to the current room. */
export function readingMeasure(
  preferences: ReadingPreferences,
  availableWidth: number | null,
) {
  const max =
    availableWidth === null
      ? 46
      : Math.min(46, Math.max(1, Math.floor(availableWidth / preferences.size)));
  const min = Math.min(26, max);
  const value = Math.min(preferences.measure, max);
  return { min, max, value, fitted: value < preferences.measure, disabled: min === max };
}

/** Observe the available container, never the changing prose width (which would feed back). */
export function useAvailableReadingWidth(
  container: RefObject<HTMLElement | null>,
  content: RefObject<HTMLElement | null>,
  fixedRightMargin = false,
  layoutKey = "",
) {
  const [width, setWidth] = useState<number | null>(null);
  useLayoutEffect(() => {
    const parent = container.current,
      element = content.current;
    if (!parent || !element) return;
    const update = () => {
      const style = getComputedStyle(element);
      const inset =
        parseFloat(style.paddingLeft) +
        parseFloat(style.paddingRight) +
        (fixedRightMargin ? parseFloat(style.marginRight) : 0);
      setWidth(Math.max(0, parent.clientWidth - inset));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(parent);
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [container, content, fixedRightMargin, layoutKey]);
  return width;
}
export function readingPreferences(value: unknown): ReadingPreferences {
  const p = (value || {}) as Partial<ReadingPreferences>;
  const clamp = (n: unknown, fallback: number, min: number, max: number) =>
    typeof n === "number" && Number.isFinite(n)
      ? Math.min(max, Math.max(min, n))
      : fallback;
  return {
    size: clamp(p.size, 18, 15, 24),
    measure: clamp(p.measure, 35, 26, 46),
    leading: clamp(p.leading, 1.9, 1.5, 2.3),
    font: p.font === "sans" ? "sans" : "serif",
  };
}
export interface ReadingPosition {
  id: string;
  fraction: number;
}
export function readingPosition(
  value: unknown,
  ids: Set<string>,
): ReadingPosition | null {
  const p = value as Partial<ReadingPosition> | null;
  return p &&
    typeof p.id === "string" &&
    ids.has(p.id) &&
    typeof p.fraction === "number" &&
    Number.isFinite(p.fraction)
    ? { id: p.id, fraction: Math.min(1, Math.max(0, p.fraction)) }
    : null;
}
export function positionKey(version: string) {
  return `paperlens:reading-position:${encodeURIComponent(version)}`;
}
export function readLocal(key: string): unknown {
  try {
    return JSON.parse(localStorage.getItem(key) || "null");
  } catch {
    return null;
  }
}
export function writeLocal(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Reading remains usable in private/offline contexts that deny storage.
  }
}
/** Parts retain the page and exact region even for cross-page paragraphs. */
export function itemSource(
  item: ReadingItem,
  blocks: Map<string, Block>,
  partIndex?: number,
): {
  page: number;
  box?: Box | null;
} {
  const part = partIndex === undefined ? undefined : item.parts[partIndex];
  const block = part ? blocks.get(part.block_id) : undefined;
  return {
    page: block?.page || item.page,
    box: part?.bbox ?? block?.bbox ?? item.bbox,
  };
}
