import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import type { DashboardReport, ReadingItem } from "./types";
import { readingDocument, readingText } from "./reading";
import { paperKey, pickedExplanation, saveTerm, termKey, useTerms } from "./terms";
import { go } from "./router";
import { ANNOTATION_COLORS } from "./annotations";
import { lastAppearance, rememberAppearance } from "./SelectionExplain";
import { MARK_SURFACE, pickReadingMark } from "./readingMarks";
import type { ReadingMark } from "./readingNotes";

/** A quotation from reflowed text anchors to its reading object, never fake PDF offsets. */
export function ReadingSelectionNote({
  report,
  paperId = "local",
  onNote,
  onAsk,
  onMark,
  rawHighlighter = false,
}: {
  report: DashboardReport;
  paperId?: string;
  onNote: (item: ReadingItem, kind: "note" | "question", quote?: string) => void;
  onAsk?: (item: ReadingItem) => void;
  onMark?: (item: ReadingItem, mark: ReadingMark) => Promise<unknown>;
  rawHighlighter?: boolean;
}) {
  const reading = useMemo(() => readingDocument(report), [report]);
  const terms = useTerms();
  const owner = paperKey(paperId, report.analysis.title);
  const [appearance, setAppearance] = useState(lastAppearance);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pick, setPick] = useState<{
    item: ReadingItem;
    text: string;
    x: number;
    y: number;
    mark: ReadingMark | null;
    context: string;
    blockId?: string;
    page: number;
  } | null>(null);
  useEffect(() => {
    const parent = (node: Node | null) =>
      node instanceof Element ? node : node?.parentElement;
    let frame = 0;
    const read = (event: Event) => {
      if (
        parent(event.target as Node)?.closest(
          ".reading-object-selection,.selection-tool,.reading-note-card,.note-composer",
        )
      )
        return;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const selection = getSelection();
        const text = selection?.toString().trim() || "";
        if (!selection?.rangeCount || !text || text.length > 4000) {
          setPick(null);
          return;
        }
        const start = parent(selection.anchorNode),
          end = parent(selection.focusNode);
        const element = start?.closest<HTMLElement>(".reading-item,.reference-card");
        if (!element || end?.closest(".reading-item,.reference-card") !== element) {
          setPick(null);
          return;
        }
        // Exact raw selections already have the original highlighter's toolbar.
        const raw = start?.closest("[data-raw-text]");
        const surface = start?.closest<HTMLElement>(MARK_SURFACE);
        if (
          rawHighlighter &&
          raw &&
          end?.closest("[data-raw-text]") === raw &&
          text.length <= 600
        ) {
          setPick(null);
          return;
        }
        const item = [...reading.items, ...reading.references].find(
          (i) => i.id === element.id,
        );
        if (!item) return;
        const rect = selection.getRangeAt(0).getBoundingClientRect();
        setError("");
        const mark = surface ? pickReadingMark(selection.getRangeAt(0), surface) : null;
        const blockId = start?.closest<HTMLElement>("[data-block-id]")?.dataset.blockId;
        const sourceText =
          mark?.text ||
          readingText(item, new Map(report.document.blocks.map((b) => [b.id, b])));
        const offset = mark?.start ?? Math.max(0, sourceText.indexOf(text));
        setPick({
          item,
          text,
          x: Math.max(10, Math.min(innerWidth - 460, rect.left)),
          y: Math.max(10, Math.min(innerHeight - 110, rect.bottom + 8)),
          mark,
          context: sourceText.slice(
            Math.max(0, offset - 120),
            offset + text.length + 120,
          ),
          blockId,
          page: report.document.blocks.find((b) => b.id === blockId)?.page || item.page,
        });
      });
    };
    const dismiss = (event: Event) => {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      setPick(null);
    };
    document.addEventListener("pointerup", read);
    document.addEventListener("keyup", read);
    window.addEventListener("scroll", dismiss, true);
    window.addEventListener("keydown", dismiss);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("pointerup", read);
      document.removeEventListener("keyup", read);
      window.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("keydown", dismiss);
    };
  }, [reading, rawHighlighter, report]);
  if (!pick) return null;
  const mark = async (
    style: ReadingMark["style"],
    color = appearance.color || "yellow",
  ) => {
    if (!pick.mark || !onMark || busy) return;
    setBusy(true);
    setError("");
    try {
      const next = { color, style };
      setAppearance(next);
      rememberAppearance(next);
      await onMark(pick.item, { ...pick.mark, ...next });
      getSelection()?.removeAllRanges();
      setPick(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const choose = (kind: "note" | "question") => {
    onNote(pick.item, kind, pick.text);
    getSelection()?.removeAllRanges();
    setPick(null);
  };
  return createPortal(
    <div
      className="reading-object-selection"
      role="toolbar"
      aria-label="选文笔记"
      style={{ left: pick.x, top: pick.y }}
      onMouseDown={(e) => e.preventDefault()}
    >
      {pick.mark && onMark && (
        <>
          <button
            className="button small"
            disabled={busy}
            onClick={() => void mark("highlight")}
          >
            高亮
          </button>
          <button
            className="button small"
            disabled={busy}
            onClick={() => void mark("underline")}
          >
            下划线
          </button>
          <div className="annotation-colors" role="group" aria-label="勾画颜色">
            {ANNOTATION_COLORS.map((c) => (
              <button
                key={c.value}
                className="annotation-swatch"
                data-color={c.value}
                aria-label={c.label}
                title={c.label}
                disabled={busy}
                onClick={() => void mark(appearance.style || "highlight", c.value)}
              />
            ))}
          </div>
        </>
      )}
      <button className="button small" onClick={() => choose("note")}>
        笔记
      </button>
      {pick.text.length <= 40 && (
        <button
          className="button small"
          onClick={() => {
            if (terms.some((t) => t.id === termKey(owner, pick.text))) {
              go({ page: "terms" });
              return;
            }
            try {
              saveTerm({
                term: pick.text,
                ...pickedExplanation(report.analysis.glossary, pick.text),
                paperId: owner,
                paperTitle: report.analysis.title,
                origin: "selection",
                quote: pick.context,
                page: pick.page,
                blockId: pick.blockId,
                itemId: pick.item.id,
              });
              setError("");
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          }}
        >
          {terms.some((t) => t.id === termKey(owner, pick.text)) ? "已收藏" : "收藏术语"}
        </button>
      )}
      {onAsk && (
        <button
          className="button small"
          onClick={() => {
            onAsk(pick.item);
            getSelection()?.removeAllRanges();
            setPick(null);
          }}
        >
          问AI
        </button>
      )}
      {error && <span role="alert">{error}</span>}
    </div>,
    document.body,
  );
}
