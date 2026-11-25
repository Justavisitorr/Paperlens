import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "./icons";
import { Markdown } from "./markdown";
import { explainSelection, type Explanation } from "./api";
import { go } from "./router";
import { jumpTo } from "./ReaderViews";
import {
  fillExplanation,
  paperKey,
  pickedExplanation,
  saveTerm,
  termKey,
  useTerms,
} from "./terms";
import type { DashboardReport } from "./types";
import type { Annotation, AnnotationAppearance } from "./api";
import { AnnotationStylePicker } from "./AnnotationCard";
import { editAnnotation, removeAnnotation } from "./annotations";
import { PASSAGE_DRAG_TYPE } from "./chat";

export interface Picked {
  block_id: string;
  start: number;
  end: number;
  text: string;
}

/** Longer picks are passages, not terms. */
const TERM_MAX = 40;
const MARGIN = 8;
const GAP = 10;
const STYLE_KEY = "paperlens.annotation-style";

/** The last colour and style the reader marked with, used for the next 勾画. */
export function lastAppearance(): AnnotationAppearance {
  try {
    const value = JSON.parse(localStorage.getItem(STYLE_KEY) || "null");
    if (value && typeof value.color === "string" && typeof value.style === "string")
      return { color: value.color, style: value.style };
  } catch {
    // Private windows and blocked storage fall back to the default.
  }
  return { color: "yellow", style: "highlight" };
}

export function rememberAppearance(value: AnnotationAppearance) {
  try {
    localStorage.setItem(STYLE_KEY, JSON.stringify(value));
  } catch {
    // Only a convenience.
  }
}

/** The sentence around a pick, so a saved term keeps its context. */
function around(text: string, start: number, end: number) {
  const stop = /[。！？；.!?;]/;
  let a = start;
  while (a > 0 && start - a < 120 && !stop.test(text[a - 1])) a--;
  let b = end;
  while (b < text.length && b - end < 120 && !stop.test(text[b])) b++;
  if (b < text.length) b++;
  return `${a > 0 ? "…" : ""}${text.slice(a, b).trim()}${b < text.length ? "…" : ""}`;
}

/**
 * The toolbar that follows a text selection in the full-text view. Native
 * selection is untouched; we read it once the pointer or key is released and
 * keep a copy of the range, so the popover stays on the words while the page
 * scrolls. Only plain text (`data-raw-text`) is pickable: formula renderings
 * are not in the block-text coordinates the service expects.
 */
export function SelectionExplain({
  paperId,
  report,
  canExplain,
  onAsk,
  onAnnotate,
  onNote,
}: {
  paperId: string;
  report: DashboardReport;
  canExplain: boolean;
  /** 问一问: take the words into the quick chat. */
  onAsk?: (picked: Picked) => void;
  onNote?: (picked: Picked, kind: "note" | "question") => void;
  /** 勾画: keep a highlight of the words. */
  onAnnotate?: (
    picked: Picked,
    appearance?: AnnotationAppearance,
  ) => Promise<Annotation | void>;
}) {
  const [picked, setPicked] = useState<Picked | null>(null);
  const [answer, setAnswer] = useState<Explanation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [appearance, setAppearance] = useState<AnnotationAppearance>(lastAppearance);
  /**
   * Set once 勾画 has drawn the mark: like EasyRead, the colours and styles are
   * offered only then, and change the mark that is already on the page.
   */
  const [made, setMade] = useState<Annotation | null>(null);
  const range = useRef<Range | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const card = useRef<HTMLDivElement>(null);
  /** Bumped on every new pick or close, so a late answer for old words is dropped. */
  const asked = useRef(0);
  const terms = useTerms();
  const expanded = Boolean(answer || error);
  const sticky = busy || expanded;

  /** `dismiss`: the reader closed it, so the highlight goes too. */
  const close = (dismiss = false) => {
    if (dismiss) getSelection()?.removeAllRanges();
    asked.current++;
    range.current = null;
    setBusy(false);
    setPicked(null);
    setAnswer(null);
    setError("");
    setMade(null);
  };

  useEffect(() => {
    const read = () => {
      // A pending answer belongs to the words it was asked about.
      if (busy) return;
      const selection = getSelection();
      const raw = selection?.rangeCount ? selection.toString() : "";
      const text = raw.trim();
      const rawHost = (node: Node) =>
        (node instanceof Element ? node : node.parentElement)?.closest<HTMLElement>(
          "[data-raw-text]",
        );
      const r = selection?.rangeCount ? selection.getRangeAt(0) : null;
      const host = r && rawHost(r.startContainer);
      if (!onAnnotate && host?.closest("[data-reading-surface]")) {
        if (!sticky && !made) close();
        return;
      }
      if (!r || !text || text.length > 600 || !host || host !== rawHost(r.endContainer)) {
        if (!sticky && !made) close();
        return;
      }
      const block = host.closest<HTMLElement>("[data-block-id]");
      if (!block) return;
      const before = document.createRange();
      before.selectNodeContents(host);
      before.setEnd(r.startContainer, r.startOffset);
      // Offsets of the trimmed text, in UTF-16 units like the service.
      const start =
        Number(host.dataset.sourceStart || 0) +
        before.toString().length +
        (raw.length - raw.trimStart().length);
      range.current = r.cloneRange();
      setAnswer(null);
      setError("");
      setMade(null);
      setPicked({
        block_id: block.dataset.blockId || "",
        start,
        end: start + text.length,
        text,
      });
    };
    const up = (event: Event) => {
      if (
        event.target instanceof Element &&
        event.target.closest(".selection-tool,.reading-object-selection,.note-composer")
      )
        return;
      setTimeout(read, 0);
    };
    const down = (e: PointerEvent) => {
      if (!sticky && box.current && !box.current.contains(e.target as Node)) close();
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape" && range.current) {
        // Consumed here: the panels behind stay open.
        e.preventDefault();
        close(true);
      }
    };
    document.addEventListener("pointerup", up);
    document.addEventListener("keyup", up);
    document.addEventListener("pointerdown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerup", up);
      document.removeEventListener("keyup", up);
      document.removeEventListener("pointerdown", down);
      document.removeEventListener("keydown", key);
    };
  }, [sticky, busy, made, onAnnotate]);

  // Anchored to the words: above the first line, or below the last one when
  // there is no room; always inside the window, arrow pointing at the pick.
  useLayoutEffect(() => {
    const el = box.current;
    const body = card.current;
    if (!picked || !el || !body) return;
    let frame = 0;
    const place = () => {
      frame = 0;
      const live = range.current?.startContainer.isConnected
        ? Array.from(range.current.getClientRects())
        : [];
      const drawn = made
        ? Array.from(document.querySelectorAll(`[data-annotation="${made.id}"]`)).flatMap(
            (m) => Array.from(m.getClientRects()),
          )
        : [];
      const rects = (live.length ? live : drawn).filter((r) => r.width || r.height);
      const pane = el.closest(".reader-main")?.getBoundingClientRect();
      // Sticky chrome at the top of the pane (the text toolbar) stays uncovered.
      const chrome = el
        .closest(".text-view,.continuous-reader")
        ?.querySelector(".text-toolbar,.continuous-toolbar");
      const ceiling = Math.max(
        MARGIN,
        (chrome?.getBoundingClientRect().bottom ?? pane?.top ?? 0) + 4,
      );
      const first = rects[0];
      const last = rects[rects.length - 1];
      if (!first || last.bottom < ceiling || (pane && first.top > pane.bottom - 8)) {
        el.style.visibility = "hidden";
        return;
      }
      // Measured at full height, then clamped again; the reader's place in a
      // long answer survives the re-measure.
      const scrolled = body.scrollTop;
      body.style.maxHeight = "";
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const vw = document.documentElement.clientWidth;
      const vh = innerHeight;
      const above = first.top - GAP - ceiling;
      const below = vh - last.bottom - GAP - MARGIN;
      const up = h <= above || (h > below && above > below);
      const room = Math.max(up ? above : below, 120);
      if (h > room) body.style.maxHeight = `${room}px`;
      body.scrollTop = scrolled;
      const height = Math.min(h, room);
      const line = up ? first : last;
      const cx = line.left + line.width / 2;
      // Inside the reading pane when it is wide enough, else inside the window.
      const [minX, maxX] =
        pane && pane.width >= w + 2 * MARGIN
          ? [pane.left + MARGIN, pane.right - MARGIN]
          : [MARGIN, vw - MARGIN];
      const left = Math.min(Math.max(cx - w / 2, minX), maxX - w);
      const top = Math.min(
        Math.max(up ? first.top - GAP - height : last.bottom + GAP, ceiling),
        vh - MARGIN - height,
      );
      el.style.left = `${left}px`;
      el.style.top = `${top}px`;
      el.style.setProperty("--arrow-x", `${Math.min(Math.max(cx - left, 16), w - 16)}px`);
      el.dataset.side = up ? "above" : "below";
      el.style.visibility = "visible";
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(place);
    };
    // Scrolling the answer itself moves nothing.
    const scrolled = (e: Event) => {
      if (!(e.target instanceof Node && el.contains(e.target))) schedule();
    };
    place();
    addEventListener("scroll", scrolled, true);
    addEventListener("resize", schedule);
    const resize = new ResizeObserver(schedule);
    resize.observe(body.firstElementChild || body);
    // The pane narrows when the 问一问 drawer opens and the words reflow.
    const pane = el.closest(".reader-main");
    if (pane) resize.observe(pane);
    return () => {
      cancelAnimationFrame(frame);
      removeEventListener("scroll", scrolled, true);
      removeEventListener("resize", schedule);
      resize.disconnect();
    };
  }, [picked, expanded, made]);

  if (!picked) return null;
  const canSave = picked.text.length <= TERM_MAX && !/\n/.test(picked.text);
  const owner = paperKey(paperId, report.analysis.title);
  const saved = terms.some((t) => t.id === termKey(owner, picked.text));
  const source = report.document.blocks.find((b) => b.id === picked.block_id);
  const snippet = `${picked.text.slice(0, 24)}${picked.text.length > 24 ? "…" : ""}`;

  const ask = async () => {
    const turn = ++asked.current;
    setBusy(true);
    setError("");
    try {
      const reply = await explainSelection(paperId, picked);
      if (turn !== asked.current) return;
      setAnswer(reply);
      // Kept before the answer came: an empty explanation in 术语本 fills in.
      if (canSave)
        try {
          fillExplanation(termKey(owner, picked.text), reply.answer, "ai", reply.model);
        } catch {
          /* the answer still shows; 术语本 keeps its own save errors */
        }
    } catch (reason) {
      if (turn === asked.current)
        setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      if (turn === asked.current) setBusy(false);
    }
  };
  /**
   * 问一问 hands the words to the quick chat. A pending or shown explanation
   * stays put, so the reader can read it beside the drawer; otherwise the
   * toolbar has done its job.
   */
  const toChat = () => {
    onAsk?.(picked);
    if (!sticky) close(true);
  };
  const annotate = () => {
    if (!onAnnotate || busy) return;
    setBusy(true);
    setError("");
    onAnnotate(picked, appearance).then(
      (created) => {
        setBusy(false);
        getSelection()?.removeAllRanges();
        if (created) setMade(created);
        else close(true);
      },
      (reason) => {
        setBusy(false);
        setError(`勾画未保存：${reason instanceof Error ? reason.message : reason}`);
      },
    );
  };
  const restyle = (next: AnnotationAppearance) => {
    if (!made || busy) return;
    setAppearance(next);
    rememberAppearance(next);
    setBusy(true);
    editAnnotation(paperId, made, next).then(
      (updated) => {
        setBusy(false);
        setMade(updated);
      },
      (reason) => {
        setBusy(false);
        setError(`勾画样式未保存：${reason instanceof Error ? reason.message : reason}`);
      },
    );
  };
  const unmark = () => {
    if (!made || busy) return;
    setBusy(true);
    removeAnnotation(paperId, made.id).then(
      () => {
        setBusy(false);
        close(true);
      },
      (reason) => {
        setBusy(false);
        setError(`勾画未删除：${reason instanceof Error ? reason.message : reason}`);
      },
    );
  };
  const chatButton = onAsk && (
    <button className="button small" title="引用到问一问" onClick={toChat}>
      <Icon name="chat" size={14} />
      问一问
    </button>
  );
  const save = () => {
    try {
      saveTerm({
        term: picked.text,
        ...pickedExplanation(report.analysis.glossary, picked.text, answer),
        paperId: owner,
        paperTitle: report.analysis.title,
        origin: "selection",
        quote: source ? around(source.text, picked.start, picked.end) : undefined,
        page: source?.page,
        blockId: picked.block_id,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const saveButton = canSave && (
    <button
      className={`button small ${saved ? "is-saved" : ""}`}
      onClick={saved ? () => go({ page: "terms" }) : save}
      title={saved ? "查看术语" : "收藏术语"}
    >
      <Icon name={saved ? "check" : "star"} size={14} />
      {saved ? "已收藏" : "收藏术语"}
    </button>
  );

  return (
    <div
      ref={box}
      className={`selection-tool ${expanded ? "is-expanded" : ""}`}
      role="dialog"
      aria-label={`所选文本“${snippet}”`}
      onPointerUp={(e) => e.stopPropagation()}
      onKeyUp={(e) => e.stopPropagation()}
    >
      <div className="selection-card" ref={card}>
        {made && !expanded ? (
          <div className="selection-marked" onMouseDown={(e) => e.preventDefault()}>
            <span className="selection-marked-head">
              <Icon name="check" size={13} />
              已勾画
            </span>
            <AnnotationStylePicker
              value={appearance}
              onChange={restyle}
              disabled={busy}
            />
            {saveButton}
            <button className="button small ghost" onClick={unmark} disabled={busy}>
              撤销
            </button>
            <button aria-label="完成" title="完成（Esc）" onClick={() => close(true)}>
              <Icon name="close" size={14} />
            </button>
          </div>
        ) : !expanded ? (
          // Keeping mouse-down from moving focus keeps the words highlighted.
          <div className="selection-actions" onMouseDown={(e) => e.preventDefault()}>
            {onNote && (
              <>
                <button
                  className="button small"
                  onClick={() => {
                    onNote(picked, "note");
                    close(true);
                  }}
                >
                  笔记
                </button>
              </>
            )}
            {canExplain && (
              <button className="button small primary" onClick={ask} disabled={busy}>
                {busy ? <span className="spinner" /> : <Icon name="sparkle" size={14} />}
                {busy ? "正在解释…" : "解释"}
              </button>
            )}
            {chatButton}
            {onAnnotate && (
              <button
                className="button small"
                title="勾画这段文字"
                disabled={busy}
                onClick={() => {
                  annotate();
                }}
              >
                <Icon name="highlighter" size={14} />
                勾画
              </button>
            )}
            {saveButton}
            {onAsk && (
              <span
                className="selection-drag"
                draggable
                role="button"
                tabIndex={0}
                title="拖入问一问输入框；也可点击引用"
                aria-label="引用所选原文到问一问"
                onMouseDown={(e) => e.stopPropagation()}
                onClick={toChat}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toChat();
                  }
                }}
                onDragStart={(e) => {
                  e.dataTransfer.effectAllowed = "copy";
                  e.dataTransfer.setData(
                    PASSAGE_DRAG_TYPE,
                    JSON.stringify({
                      kind: "passage",
                      paper_id: paperId,
                      paper_title: report.analysis.title,
                      ...picked,
                      page: source?.page,
                    }),
                  );
                  e.dataTransfer.setData("text/plain", picked.text);
                }}
              >
                拖入问一问
              </span>
            )}
            {!canExplain && !canSave && !onAsk && !onAnnotate && !onNote && (
              <span className="selection-hint">选中不超过 {TERM_MAX} 字可收藏为术语</span>
            )}
          </div>
        ) : (
          <div className="selection-answer">
            <div className="popover-head">
              <span>“{snippet}”</span>
              <button aria-label="关闭" title="关闭（Esc）" onClick={() => close(true)}>
                <Icon name="close" size={14} />
              </button>
            </div>
            {error ? (
              <p className="error-line" role="alert">
                {error}
              </p>
            ) : (
              <>
                {answer?.answer && <Markdown text={answer.answer} />}
                {answer?.grounded === false && (
                  <p className="selection-warn">
                    <Icon name="warning" size={13} />
                    回答中的引句没能在原文中匹配，请谨慎参考。
                  </p>
                )}
                {answer?.warnings?.map((w, i) => (
                  <p className="selection-warn" key={i}>
                    <Icon name="info" size={13} />
                    {w}
                  </p>
                ))}
                {!!answer?.quotes?.length && (
                  <ul className="selection-quotes" aria-label="原文依据">
                    {answer.quotes.map((q, i) => (
                      <li key={i}>
                        <button onClick={() => jumpTo(`block-${q.block_id}`)}>
                          <span>P{q.page}</span>
                          {q.quote}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
            <div className="selection-foot">
              <small>由本地服务调用模型生成，请结合原文判断。</small>
              <span className="selection-foot-actions">
                {chatButton}
                {error.startsWith("勾画未保存") && onAnnotate && (
                  <button className="button small" onClick={annotate} disabled={busy}>
                    重试保存勾画
                  </button>
                )}
                {!error && saveButton}
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
