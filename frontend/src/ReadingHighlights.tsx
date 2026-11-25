import { SaveSelectedTerm } from "./SaveSelectedTerm";
import { paperKey, pickedExplanation } from "./terms";
import { readingDocument } from "./reading";
import type { DashboardReport } from "./types";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { AnnotationStylePicker } from "./AnnotationCard";
import { rememberAppearance } from "./SelectionExplain";
import type { AnnotationAppearance } from "./api";
import type { ReadingNote } from "./readingNotes";
import { MARK_SURFACE, readingMarkRange } from "./readingMarks";
import "./styles/25-reading-highlights.css";

/** Paint ranges without rewriting React's text nodes, links or MathML. */
export function ReadingHighlights({
  report,
  paperId,
  notes,
  onEdit,
  onChange,
  onDelete,
}: {
  report: DashboardReport;
  paperId: string;
  notes: ReadingNote[];
  onEdit: (note: ReadingNote) => void;
  onChange: (note: ReadingNote, appearance: AnnotationAppearance) => Promise<unknown>;
  onDelete: (note: ReadingNote) => Promise<unknown>;
}) {
  const [opened, setOpened] = useState<{ id: string; x: number; y: number } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const root = document.querySelector(".continuous-prose");
    if (!root) return;
    const registry = CSS.highlights;
    const names = new Set<string>();
    let painted: { note: ReadingNote; range: Range }[] = [];
    const clear = () => {
      names.forEach((key) => registry.delete(key));
      names.clear();
    };
    const paint = () => {
      clear();
      painted = [];
      for (const note of notes) {
        if (!note.mark || note.detached) continue;
        const item = document.getElementById(note.item_id);
        for (const surface of item?.querySelectorAll<HTMLElement>(MARK_SURFACE) || []) {
          const range = readingMarkRange(surface, note.mark);
          if (!range) continue;
          const key = `reading-${note.mark.style}-${note.mark.color}`;
          if (!names.has(key)) {
            names.add(key);
            registry.set(key, new Highlight());
          }
          registry.get(key)!.add(range);
          painted.push({ note, range });
        }
      }
    };
    paint();
    let frame = 0;
    const observer = new MutationObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(paint);
    });
    observer.observe(root, { childList: true, subtree: true, characterData: true });
    const click = (event: MouseEvent) => {
      if (getSelection()?.toString().trim()) return;
      const target = event.target as HTMLElement;
      if (target.closest("button,a,.reading-mark-menu")) return;
      const hit = [...painted]
        .reverse()
        .find(({ range }) =>
          [...range.getClientRects()].some(
            (r) =>
              event.clientX >= r.left &&
              event.clientX <= r.right &&
              event.clientY >= r.top &&
              event.clientY <= r.bottom,
          ),
        );
      if (!hit) return;
      event.stopPropagation();
      setError("");
      setOpened({
        id: hit.note.id,
        x: Math.max(8, Math.min(innerWidth - 320, event.clientX)),
        y: Math.max(8, Math.min(innerHeight - 165, event.clientY + 12)),
      });
    };
    root.addEventListener("click", click as EventListener, true);
    return () => {
      clear();
      observer.disconnect();
      cancelAnimationFrame(frame);
      root.removeEventListener("click", click as EventListener, true);
    };
  }, [notes]);
  useEffect(() => {
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent) {
        if (event.key !== "Escape") return;
      } else if ((event.target as Element)?.closest?.(".reading-mark-menu")) return;
      setOpened(null);
    };
    document.addEventListener("pointerdown", close);
    window.addEventListener("scroll", close, true);
    window.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("keydown", close);
    };
  }, []);
  const note = notes.find((n) => n.id === opened?.id);
  if (!opened || !note?.mark) return null;
  const item = readingDocument(report).items.find((item) => item.id === note.item_id);
  const selected = note.mark.text.slice(note.mark.start, note.mark.end).trim();
  const blockIds = [...new Set(item?.parts.map((part) => part.block_id) || [])];
  return createPortal(
    <div
      className="reading-mark-menu"
      role="dialog"
      aria-label="编辑勾画"
      style={{ left: opened.x, top: opened.y }}
    >
      <AnnotationStylePicker
        value={note.mark}
        onChange={(value) => {
          rememberAppearance(value);
          void onChange(note, value).catch((e) => setError(String(e)));
        }}
      />
      <div className="reading-note-actions">
        <SaveSelectedTerm
          key={note.id}
          term={{
            term: selected,
            ...pickedExplanation(report.analysis.glossary, selected),
            origin: "selection",
            paperId: paperKey(paperId, report.analysis.title),
            paperTitle: report.analysis.title,
            quote: note.mark.text.slice(
              Math.max(0, note.mark.start - 120),
              note.mark.end + 120,
            ),
            itemId: note.item_id,
            page: note.page,
            blockId: blockIds.length === 1 ? blockIds[0] : undefined,
          }}
        />
        <button
          className="button small"
          onClick={() => {
            onEdit(note);
            setOpened(null);
          }}
        >
          写笔记
        </button>
        <button
          className="button small"
          onClick={() =>
            void onDelete(note)
              .then(() => setOpened(null))
              .catch((e) => setError(String(e)))
          }
        >
          删除勾画
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
    </div>,
    document.body,
  );
}
