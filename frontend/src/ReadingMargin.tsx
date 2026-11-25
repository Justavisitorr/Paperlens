import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import "./styles/24-reading-margin.css";

export interface NoteGroup {
  itemId: string;
  body: ReactNode;
}

export function useNoteMargin(
  column: RefObject<HTMLDivElement | null>,
  enabled: boolean,
) {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = column.current;
    if (!element) return;
    const update = () => setWidth(element.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [column]);
  return enabled && width >= 1060;
}

/** Margin cards follow their actual anchors without adding gaps to the paper. */
export function ReadingMargin({
  column,
  groups,
}: {
  column: RefObject<HTMLDivElement | null>;
  groups: NoteGroup[];
}) {
  const rail = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const parent = column.current,
      element = rail.current;
    if (!parent || !element) return;
    let frame = 0;
    const place = () => {
      frame = 0;
      const origin = parent.getBoundingClientRect().top;
      const cards = Array.from(element.children) as HTMLElement[];
      const positions = cards
        .map((card) => ({
          card,
          anchor: document.getElementById(card.dataset.anchor || ""),
        }))
        .filter((entry) => entry.anchor && entry.anchor.getClientRects().length)
        .map((entry) => ({
          ...entry,
          top: entry.anchor!.getBoundingClientRect().top - origin,
        }))
        .sort((a, b) => a.top - b.top);
      cards.forEach((card) => {
        card.style.visibility = "hidden";
      });
      let bottom = 0;
      for (const { card, top } of positions) {
        const y = Math.max(top, bottom);
        card.style.top = `${Math.round(y)}px`;
        card.style.visibility = "visible";
        bottom = y + card.offsetHeight + 16;
      }
      parent.style.setProperty("--notes-height", `${Math.ceil(bottom + 40)}px`);
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(place);
    };
    const observer = new ResizeObserver(schedule);
    const prose = parent.querySelector(".continuous-prose");
    if (prose) observer.observe(prose);
    Array.from(element.children).forEach((child) => observer.observe(child));
    place();
    void document.fonts.ready.then(schedule);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      parent.style.removeProperty("--notes-height");
    };
  }, [groups, column]);
  return (
    <aside className="reading-note-margin" aria-label="页边笔记" ref={rail}>
      {groups.map((group) => (
        <div key={group.itemId} className="reading-note-group" data-anchor={group.itemId}>
          {group.body}
        </div>
      ))}
    </aside>
  );
}
