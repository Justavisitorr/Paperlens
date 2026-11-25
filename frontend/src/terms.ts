import { useSyncExternalStore } from "react";
import { markdownQuote } from "./markdownExport";

/**
 * “术语本”: terms the reader keeps across papers. It lives in this
 * browser storage; standalone hosting synchronizes that storage per account.
 * A downloaded offline file remains independent and uses only local storage.
 */
export interface SavedTerm {
  id: string;
  term: string;
  explanation: string;
  paperId: string;
  paperTitle: string;
  /** Where it was collected: a glossary entry or a text selection. */
  origin: "glossary" | "selection";
  quote?: string;
  page?: number;
  blockId?: string;
  /** Reflowed/translated selections anchor to a reading object, not invented raw offsets. */
  itemId?: string;
  note?: string;
  /** Who wrote the explanation; absent on records saved before this field. */
  explanationBy?: "paper" | "ai" | "me";
  /** The model behind an AI explanation, when the service reported one. */
  explanationModel?: string;
  added: string;
  updated?: string;
}

const KEY = "paperlens.terms";
const listeners = new Set<() => void>();
let cache: SavedTerm[] | null = null;

function load(): SavedTerm[] {
  if (cache) return cache;
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "[]");
    cache = Array.isArray(raw) ? normalizeTerms(raw) : [];
  } catch {
    cache = [];
  }
  return cache;
}
/**
 * Records come from older builds, other tabs and hand edits: missing strings
 * become empty, blank notes disappear, and duplicates keep the newest copy
 * with whichever explanation and note it lacked.
 */
export function normalizeTerms(raw: unknown[]): SavedTerm[] {
  const out = new Map<string, SavedTerm>();
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const t = entry as Partial<SavedTerm>;
    const term = typeof t.term === "string" ? t.term.trim() : "";
    const paperId = typeof t.paperId === "string" ? t.paperId : "";
    if (!term || !paperId) continue;
    const id = typeof t.id === "string" && t.id ? t.id : termKey(paperId, term);
    const note = typeof t.note === "string" && t.note.trim() ? t.note : undefined;
    const clean: SavedTerm = {
      ...(t as SavedTerm),
      id,
      term,
      paperId,
      paperTitle: typeof t.paperTitle === "string" ? t.paperTitle : "",
      explanation: typeof t.explanation === "string" ? t.explanation : "",
      origin: t.origin === "glossary" ? "glossary" : "selection",
      note,
      added: typeof t.added === "string" ? t.added : new Date(0).toISOString(),
    };
    const seen = out.get(id);
    if (!seen) out.set(id, clean);
    else {
      seen.explanation ||= clean.explanation;
      seen.note ??= clean.note;
    }
  }
  return [...out.values()];
}
function commit(next: SavedTerm[], requireStorage = false) {
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    if (requireStorage)
      throw new Error("术语未能保存到浏览器，请检查存储空间或浏览器权限后重试。");
  }
  cache = next;
  listeners.forEach((notify) => notify());
}
function subscribe(notify: () => void) {
  listeners.add(notify);
  // Another tab changed the library.
  const onStorage = (e: StorageEvent) => {
    if (e.key !== KEY) return;
    cache = null;
    notify();
  };
  addEventListener("storage", onStorage);
  return () => {
    listeners.delete(notify);
    removeEventListener("storage", onStorage);
  };
}

/**
 * Every offline file calls its one paper "local"; the title keeps terms from
 * different offline papers apart.
 */
export const paperKey = (paperId: string, title: string) =>
  paperId === "local" ? `local:${title}` : paperId;
/** The reader route of a saved term's paper. */
export const routePaperId = (key: string) => (key.startsWith("local:") ? "local" : key);

export const termKey = (paperId: string, term: string) =>
  `${paperId}::${term.trim().toLocaleLowerCase()}`;

/** Newest first. */
export function useTerms(): SavedTerm[] {
  return useSyncExternalStore(subscribe, load);
}

export function saveTerm(term: Omit<SavedTerm, "id" | "added">): SavedTerm {
  const saved: SavedTerm = {
    ...term,
    term: term.term.trim(),
    id: termKey(term.paperId, term.term),
    added: new Date().toISOString(),
  };
  if (saved.explanation && !saved.explanationBy)
    saved.explanationBy = term.origin === "glossary" ? "paper" : "ai";
  const old = load().find((t) => t.id === saved.id);
  // Re-adding keeps the reader's note, and an explanation they wrote or
  // already had wins over an empty or machine one.
  if (old) {
    saved.note = old.note;
    if (old.explanation && (!saved.explanation || old.explanationBy === "me")) {
      saved.explanation = old.explanation;
      saved.explanationBy = old.explanationBy;
      saved.explanationModel = old.explanationModel;
    }
  }
  commit([saved, ...load().filter((t) => t.id !== saved.id)], true);
  return saved;
}

export type TermPatch = Partial<
  Pick<SavedTerm, "note" | "explanation" | "explanationBy" | "explanationModel">
>;
/** Throws when the browser refuses to store it, so editors can keep the draft. */
export function updateTerm(id: string, patch: TermPatch) {
  const next = { ...patch };
  if ("note" in next && !next.note?.trim()) next.note = undefined;
  if ("explanation" in next && !next.explanation?.trim()) {
    next.explanation = "";
    next.explanationBy = undefined;
    next.explanationModel = undefined;
  }
  commit(
    load().map((t) =>
      t.id === id ? { ...t, ...next, updated: new Date().toISOString() } : t,
    ),
    true,
  );
}

/**
 * An explanation that arrives after the term was kept (the selection
 * popover, the glossary) fills an empty one; it never overwrites words the
 * reader wrote or an explanation already there.
 */
export function fillExplanation(
  id: string,
  explanation: string,
  by: "paper" | "ai",
  model?: string,
) {
  const old = load().find((t) => t.id === id);
  if (!old || old.explanation.trim() || !explanation.trim()) return;
  updateTerm(id, { explanation, explanationBy: by, explanationModel: model });
}

/** The paper's own glossary entry for a picked word, matched like the key. */
export function glossaryMatch(
  glossary: { term: string; explanation: string }[],
  text: string,
): string {
  const want = text.trim().toLocaleLowerCase();
  return (
    glossary.find((g) => g.term.trim().toLocaleLowerCase() === want)?.explanation || ""
  );
}

/**
 * The explanation a picked word starts with: the paper's glossary entry when
 * the word is one, otherwise an AI answer already on screen, otherwise none.
 */
export function pickedExplanation(
  glossary: { term: string; explanation: string }[],
  text: string,
  ai?: { answer?: string; model?: string } | null,
): Pick<SavedTerm, "explanation" | "explanationBy" | "explanationModel"> {
  const paper = glossaryMatch(glossary, text);
  if (paper) return { explanation: paper, explanationBy: "paper" };
  if (ai?.answer?.trim())
    return { explanation: ai.answer, explanationBy: "ai", explanationModel: ai.model };
  return { explanation: "" };
}

/** The question an AI explanation of a kept term asks. */
export const explainQuestion = (t: Pick<SavedTerm, "term" | "quote" | "paperTitle">) =>
  [
    `请结合论文《${t.paperTitle}》的上下文，用通俗中文解释术语“${t.term}”：`,
    "先给一句话定义，再说明它在本文中的作用；涉及符号时核对定义与适用条件，数学用 $…$ 书写。",
    t.quote ? `出现的原文：${t.quote}` : "",
  ]
    .filter(Boolean)
    .join("\n");

export function removeTerms(ids: string[]) {
  const gone = new Set(ids);
  commit(load().filter((t) => !gone.has(t.id)));
}

/** Puts terms back where they were, e.g. after an undo. */
export function restoreTerms(terms: SavedTerm[]) {
  const ids = new Set(terms.map((t) => t.id));
  commit(
    [...terms, ...load().filter((t) => !ids.has(t.id))].sort((a, b) =>
      b.added.localeCompare(a.added),
    ),
  );
}

/**
 * A place to land after navigating to a reader view: the term library links
 * to a glossary entry or a text block, and the view scrolls there on mount.
 */
let pending: { mode: "glossary" | "text"; target: string } | null = null;
export function jumpLater(mode: "glossary" | "text", target: string) {
  pending = { mode, target };
}
export function takeJump(mode: "glossary" | "text"): string | null {
  if (pending?.mode !== mode) return null;
  const target = pending.target;
  pending = null;
  return target;
}

const csvCell = (value: string) => `"${value.replace(/"/g, '""')}"`;
export function termsCsv(terms: SavedTerm[]): string {
  const rows = [["术语", "解释", "笔记", "论文", "页码", "原文", "加入时间"]];
  for (const t of terms)
    rows.push([
      t.term,
      t.explanation,
      t.note || "",
      t.paperTitle,
      t.page ? String(t.page) : "",
      t.quote || "",
      t.added.slice(0, 10),
    ]);
  // BOM so spreadsheet apps read the Chinese text as UTF-8.
  return "\uFEFF" + rows.map((r) => r.map(csvCell).join(",")).join("\r\n");
}

const byLabel = { paper: "论文术语表", ai: "AI 解释", me: "我的解释" } as const;
export const explanationLabel = (t: SavedTerm) =>
  t.explanationBy
    ? byLabel[t.explanationBy]
    : t.origin === "glossary"
      ? byLabel.paper
      : "";

export function termsMarkdown(terms: SavedTerm[]): string {
  const lines = ["# 术语本", ""];
  for (const t of terms) {
    lines.push(`## ${t.term}`, "");
    if (t.explanation) {
      const label = explanationLabel(t);
      lines.push(t.explanation, "");
      if (label)
        lines.push(
          `*${label}${t.explanationModel ? ` · ${t.explanationModel}` : ""}*`,
          "",
        );
    }
    if (t.note) lines.push(markdownQuote(`笔记：${t.note}`), "");
    if (t.quote)
      lines.push(markdownQuote(`原文${t.page ? `（P${t.page}）` : ""}：${t.quote}`), "");
    lines.push(`*来源：《${t.paperTitle}》 · ${t.added.slice(0, 10)}*`, "");
  }
  return lines.join("\n");
}
