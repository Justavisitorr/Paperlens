import type { IconName } from "./icons";
import type { ReaderMode } from "./router";
import { normalizePrefs } from "./prefs";

/** Navigation, keyboard order, and help share one source. */
export const READING_MODES: [ReaderMode, IconName, string][] = [
  ["overview", "lens", "阅读总览"],
  ["map", "graph", "导图"],
  ["text", "file", "全文"],
  ["glossary", "book", "术语"],
  ["quality", "shield", "处理记录"],
];

export function savedFullReadMode(): "map" | "text" {
  try {
    return normalizePrefs(JSON.parse(localStorage.getItem("paperlens.prefs") || "{}"))
      .fullReadMode;
  } catch {
    return "text";
  }
}
