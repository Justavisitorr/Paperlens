import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "./icons";
import {
  ANNOTATION_COLORS,
  editAnnotation,
  readAnnotationDraft,
  removeAnnotation,
  saveAnnotationDraft,
} from "./annotations";
import type { Annotation, AnnotationAppearance } from "./api";

const MARGIN = 8;

const clip = (text: string, n: number) =>
  text.length > n ? `${text.slice(0, n)}…` : text;

export function AnnotationStylePicker({
  value,
  onChange,
  disabled = false,
}: {
  value: AnnotationAppearance;
  onChange: (next: AnnotationAppearance) => void;
  disabled?: boolean;
}) {
  return (
    <div className="annotation-picker" aria-label="勾画样式">
      <div className="annotation-colors" role="group" aria-label="勾画颜色">
        {ANNOTATION_COLORS.map((c) => (
          <button
            key={c.value}
            type="button"
            className="annotation-swatch"
            data-color={c.value}
            aria-label={c.label}
            title={c.label}
            aria-pressed={(value.color || "yellow") === c.value}
            disabled={disabled}
            onClick={() =>
              onChange({ color: c.value, style: value.style || "underline" })
            }
          >
            {(value.color || "yellow") === c.value && <Icon name="check" size={12} />}
          </button>
        ))}
      </div>
      <div role="group" aria-label="勾画类型">
        {(
          [
            ["highlight", "高亮"],
            ["underline", "下划线"],
          ] as const
        ).map(([style, label]) => (
          <button
            key={style}
            className="button small"
            aria-pressed={(value.style || "underline") === style}
            disabled={disabled}
            onClick={() => onChange({ color: value.color || "yellow", style })}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * The card for one 勾画, opened by clicking the painted words: a note, and
 * the ways onward (ask about it, copy it, drop it).
 */
export function AnnotationCard({
  paperId,
  annotation,
  anchor,
  onAsk,
  onClose,
  onToast,
}: {
  paperId: string;
  annotation: Annotation;
  anchor: HTMLElement;
  onAsk?: (annotation: Annotation) => void;
  onClose: () => void;
  onToast: (message: string) => void;
}) {
  const [draft] = useState(() => readAnnotationDraft(paperId, annotation.id));
  const [note, setNote] = useState(draft?.note ?? annotation.note ?? "");
  const [appearance, setAppearance] = useState<AnnotationAppearance>({
    color: draft?.color ?? annotation.color ?? "yellow",
    style: draft?.style ?? annotation.style ?? "underline",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [draftSaved, setDraftSaved] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  const changed =
    note !== (annotation.note || "") ||
    appearance.color !== (annotation.color || "yellow") ||
    appearance.style !== (annotation.style || "underline");

  useEffect(() => {
    setDraftSaved(
      saveAnnotationDraft(
        paperId,
        annotation.id,
        changed ? { note, ...appearance } : null,
      ),
    );
  }, [paperId, annotation.id, note, appearance, changed]);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      onClose();
    };
    const down = (e: PointerEvent) => {
      const target = e.target as Node;
      if (!box.current?.contains(target) && !anchor.contains(target)) onClose();
    };
    document.addEventListener("keydown", key);
    document.addEventListener("pointerdown", down);
    return () => {
      document.removeEventListener("keydown", key);
      document.removeEventListener("pointerdown", down);
    };
  }, [anchor, onClose]);

  // Below the painted words, or above them near the bottom of the window.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    let frame = 0;
    const place = () => {
      frame = 0;
      if (!anchor.isConnected) return onClose();
      const rects = Array.from(anchor.getClientRects());
      const first = rects[0] || anchor.getBoundingClientRect();
      const last = rects[rects.length - 1] || first;
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const vw = document.documentElement.clientWidth;
      const below = innerHeight - last.bottom - MARGIN;
      const top = below >= h + 8 ? last.bottom + 8 : Math.max(MARGIN, first.top - h - 8);
      const left = Math.min(Math.max(first.left, MARGIN), vw - w - MARGIN);
      el.style.top = `${top}px`;
      el.style.left = `${left}px`;
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(place);
    };
    place();
    addEventListener("scroll", schedule, true);
    addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(frame);
      removeEventListener("scroll", schedule, true);
      removeEventListener("resize", schedule);
    };
  }, [anchor, onClose]);

  const act = async (work: () => Promise<unknown>, done: string, close = false) => {
    setBusy(true);
    setError("");
    try {
      await work();
      onToast(done);
      if (close) onClose();
    } catch (e) {
      setError(`未保存：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    await editAnnotation(paperId, annotation, { note, ...appearance });
    saveAnnotationDraft(paperId, annotation.id, null);
  };

  return (
    <div
      ref={box}
      className="annotation-card popover"
      role="dialog"
      aria-label="勾画"
      onPointerUp={(e) => e.stopPropagation()}
      onKeyUp={(e) => e.stopPropagation()}
    >
      <blockquote>
        <span className="page-badge">P{annotation.page}</span>
        {clip(annotation.selection.text, 160)}
      </blockquote>
      <AnnotationStylePicker
        value={appearance}
        onChange={setAppearance}
        disabled={busy}
      />
      <textarea
        value={note}
        rows={2}
        maxLength={2000}
        placeholder="写一句笔记（可选）"
        aria-label="勾画笔记"
        disabled={busy}
        onChange={(e) => setNote(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && changed)
            void act(save, "笔记已保存", true);
        }}
      />
      {changed && (
        <small role="status">
          {draftSaved
            ? "草稿已暂存于本浏览器；点击保存写入论文笔记。"
            : "草稿暂存失败，请保持此窗口打开并保存。"}
        </small>
      )}
      {error && (
        <p className="error-line" role="alert">
          {error}
        </p>
      )}
      <div className="annotation-actions">
        {changed ? (
          <button
            className="button small primary"
            disabled={busy}
            onClick={() => void act(save, "笔记已保存", true)}
          >
            <Icon name="check" size={14} />
            保存勾画
          </button>
        ) : (
          onAsk && (
            <button
              className="button small"
              onClick={() => {
                onAsk(annotation);
                onClose();
              }}
            >
              <Icon name="chat" size={14} />
              问一问
            </button>
          )
        )}
        <button
          className="icon-button small"
          title="复制原文"
          aria-label="复制原文"
          onClick={() =>
            void act(
              () => navigator.clipboard.writeText(annotation.selection.text),
              "已复制",
            )
          }
        >
          <Icon name="copy" size={14} />
        </button>
        <button
          className="icon-button small danger"
          title="删除勾画"
          aria-label="删除勾画"
          disabled={busy}
          onClick={() =>
            void act(() => removeAnnotation(paperId, annotation.id), "已删除勾画", true)
          }
        >
          <Icon name="trash" size={14} />
        </button>
      </div>
    </div>
  );
}
