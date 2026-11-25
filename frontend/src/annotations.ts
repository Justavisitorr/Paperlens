import { useEffect, useSyncExternalStore } from "react";
import {
  createAnnotation,
  deleteAnnotation,
  listAnnotations,
  updateAnnotation,
  type Annotation,
  type AnnotationAppearance,
  type Selection,
} from "./api";
import type { DashboardReport } from "./types";
import { markdownQuote } from "./markdownExport";

/**
 * 勾画: highlights kept by the local service, one list per paper. The
 * edits keep the original ID, timestamp and source anchors.
 */
interface Entry {
  items: Annotation[] | null;
  error: string;
  loading?: Promise<Annotation[]>;
}
const store = new Map<string, Entry>();
const listeners = new Set<() => void>();
let version = 0;
const emit = () => {
  version++;
  listeners.forEach((notify) => notify());
};
const subscribe = (notify: () => void) => {
  listeners.add(notify);
  return () => listeners.delete(notify);
};
const EMPTY: Entry = { items: null, error: "" };

function set(paperId: string, entry: Entry) {
  store.set(paperId, entry);
  emit();
}
const byPosition = (list: Annotation[]) =>
  [...list].sort(
    (a, b) =>
      a.page - b.page ||
      a.selection.block_id.localeCompare(b.selection.block_id, undefined, {
        numeric: true,
      }) ||
      a.selection.start - b.selection.start,
  );

export function loadAnnotations(paperId: string): Promise<Annotation[]> {
  const entry = store.get(paperId);
  if (entry?.items) return Promise.resolve(entry.items);
  if (entry?.loading) return entry.loading;
  const loading = listAnnotations(paperId).then(
    (items) => {
      set(paperId, { items: byPosition(items), error: "" });
      return store.get(paperId)!.items!;
    },
    (e) => {
      set(paperId, { items: null, error: e instanceof Error ? e.message : String(e) });
      return [];
    },
  );
  set(paperId, { ...(entry || EMPTY), loading });
  return loading;
}

export function useAnnotations(paperId: string, enabled: boolean): Entry {
  useEffect(() => {
    if (enabled) void loadAnnotations(paperId);
  }, [paperId, enabled]);
  const entry = useSyncExternalStore(subscribe, () => store.get(paperId) || EMPTY);
  return enabled ? entry : EMPTY;
}

/** Highlights already loaded for these papers (for the reference picker). */
export function useLoadedAnnotations(paperIds: string[], enabled: boolean) {
  useSyncExternalStore(subscribe, () => version);
  const key = paperIds.join(" ");
  useEffect(() => {
    if (enabled)
      key
        .split(" ")
        .filter(Boolean)
        .forEach((id) => void loadAnnotations(id));
  }, [key, enabled]);
  return enabled
    ? paperIds.flatMap((id) =>
        (store.get(id)?.items || []).map((a) => ({ paperId: id, a })),
      )
    : [];
}

export async function addAnnotation(
  paperId: string,
  selection: Selection,
  note = "",
  appearance: AnnotationAppearance = {},
): Promise<Annotation> {
  const created = await createAnnotation(paperId, { ...selection, note, ...appearance });
  const items = store.get(paperId)?.items || [];
  set(paperId, { items: byPosition([...items, created]), error: "" });
  return created;
}

export async function removeAnnotation(paperId: string, id: string) {
  await deleteAnnotation(paperId, id);
  const items = store.get(paperId)?.items || [];
  set(paperId, { items: items.filter((a) => a.id !== id), error: "" });
}

export async function editAnnotation(
  paperId: string,
  old: Annotation,
  patch: AnnotationAppearance & { note?: string },
) {
  const next = await updateAnnotation(paperId, old.id, patch);
  const items = (store.get(paperId)?.items || []).filter((a) => a.id !== old.id);
  set(paperId, { items: byPosition([...items, next]), error: "" });
  return next;
}

export const ANNOTATION_COLORS = [
  { value: "yellow", label: "黄色" },
  { value: "green", label: "绿色" },
  { value: "blue", label: "蓝色" },
  { value: "pink", label: "粉色" },
] as const;

export interface AnnotationDraft extends AnnotationAppearance {
  note: string;
}
const draftKey = (paperId: string, id: string) =>
  `paperlens:annotation-draft:${encodeURIComponent(paperId)}:${id}`;
export function readAnnotationDraft(paperId: string, id: string): AnnotationDraft | null {
  try {
    const value = JSON.parse(localStorage.getItem(draftKey(paperId, id)) || "null");
    return value &&
      typeof value.note === "string" &&
      ANNOTATION_COLORS.some((c) => c.value === value.color) &&
      ["highlight", "underline"].includes(value.style)
      ? value
      : null;
  } catch {
    return null;
  }
}
export function saveAnnotationDraft(
  paperId: string,
  id: string,
  draft: AnnotationDraft | null,
) {
  try {
    if (draft) localStorage.setItem(draftKey(paperId, id), JSON.stringify(draft));
    else localStorage.removeItem(draftKey(paperId, id));
    return true;
  } catch {
    return false;
  }
}

/** Source order comes from blocks, never from alphabetic IDs or creation time. */
export function annotationsInSourceOrder(items: Annotation[], report?: DashboardReport) {
  if (!report) return byPosition(items);
  const order = new Map(report.document.blocks.map((b, i) => [b.id, i]));
  return [...items].sort(
    (a, b) =>
      (order.get(a.selection.block_id) ?? Infinity) -
        (order.get(b.selection.block_id) ?? Infinity) ||
      a.selection.start - b.selection.start ||
      a.created_at.localeCompare(b.created_at),
  );
}

export function annotationsMarkdown(items: Annotation[], report?: DashboardReport) {
  const lines = [`# ${report?.analysis.title || "论文"} · 阅读笔记`, ""];
  if (report) lines.push(`来源文件：${report.document.source_name}`, "");
  for (const a of annotationsInSourceOrder(items, report)) {
    const color = ANNOTATION_COLORS.find((c) => c.value === (a.color || "yellow"))!.label;
    lines.push(
      `## PDF 第 ${a.page} 页${a.section ? ` · ${a.section}` : ""}`,
      "",
      markdownQuote(a.exact || a.selection.text),
      "",
      `标记：${color} · ${a.style === "highlight" ? "高亮" : "下划线"}`,
      `原文定位：${a.selection.block_id}，UTF-16 [${a.selection.start}, ${a.selection.end})`,
      `来源版本：${a.source_version || "旧记录未保存版本"}`,
      ...(a.document_sha256 ? [`文档 SHA-256：${a.document_sha256}`] : []),
      "",
      ...(a.note ? [a.note, ""] : []),
    );
  }
  return lines.join("\n");
}

/* ---------- rendering: search hit + highlights over one block's text ---------- */

export interface Segment {
  text: string;
  /** Part of the search match. */
  hit: boolean;
  /** The highlight covering this piece, if any. */
  mark: string | null;
}

/**
 * Cuts `text` into pieces at every search-hit and highlight boundary.
 * Joining the pieces gives `text` back unchanged, so selection offsets
 * measured on the rendered paragraph stay block-text offsets. Overlapping
 * highlights: the one that starts first wins the overlap.
 */
export function segments(
  text: string,
  query: string,
  marks: { id: string; start: number; end: number }[],
): Segment[] {
  const q = query.trim().toLocaleLowerCase();
  const hitAt = q ? text.toLocaleLowerCase().indexOf(q) : -1;
  const hit = hitAt < 0 ? null : [hitAt, hitAt + q.length];
  const ranges = marks
    .filter((m) => m.end > m.start && m.start < text.length)
    .map((m) => ({ ...m, end: Math.min(m.end, text.length) }))
    .sort((a, b) => a.start - b.start);
  const cuts = new Set([0, text.length]);
  if (hit) hit.forEach((c) => cuts.add(c));
  for (const m of ranges) cuts.add(m.start).add(m.end);
  const points = [...cuts].sort((a, b) => a - b);
  const out: Segment[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const [a, b] = [points[i], points[i + 1]];
    if (a === b) continue;
    const piece: Segment = {
      text: text.slice(a, b),
      hit: Boolean(hit && a >= hit[0] && b <= hit[1]),
      mark: ranges.find((m) => m.start <= a && m.end >= b)?.id ?? null,
    };
    const last = out[out.length - 1];
    if (last && last.hit === piece.hit && last.mark === piece.mark)
      last.text += piece.text;
    else out.push(piece);
  }
  return out;
}
