import { useEffect, useRef, useState } from "react";
import { RichText } from "./readerLinks";
import { AssetImage } from "./AssetImage";
import { createPortal } from "react-dom";
import { useFocusTrap } from "./dialog";
import {
  imageFit,
  imagePan,
  imageZoom,
  type ImagePan,
  type ImageSize,
} from "./imageViewerGeometry";
import "./styles/image-viewer.css";

export interface ImageViewerProps {
  src: string;
  alt: string;
  onClose: () => void;
  onSource?: () => void;
}

/** Display-only transforms. The source asset and evidence coordinates never change. */
export function ImageViewer({ src, alt, onClose, onSource }: ImageViewerProps) {
  const dialog = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; x: number; y: number; pan: ImagePan } | null>(null);
  const [natural, setNatural] = useState<ImageSize>({ width: 0, height: 0 });
  const [view, setView] = useState<ImageSize>({ width: 1, height: 1 });
  const [zoom, setZoom] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [flipX, setFlipX] = useState(false),
    [flipY, setFlipY] = useState(false);
  const [pan, setPan] = useState<ImagePan>({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false),
    [error, setError] = useState(false);
  useFocusTrap(dialog);
  const fit = imageFit(natural, view, rotation);
  const scale = fit * zoom;
  const constrained = imagePan(pan, natural, view, rotation, scale);
  const maxPan = imagePan({ x: Infinity, y: Infinity }, natural, view, rotation, scale);
  const canDrag = maxPan.x > 0 || maxPan.y > 0;
  const changeZoom = (value: number) => {
    const next = imageZoom(value);
    setPan(
      imagePan(
        { x: (constrained.x * next) / zoom, y: (constrained.y * next) / zoom },
        natural,
        view,
        rotation,
        fit * next,
      ),
    );
    setZoom(next);
  };
  const reset = () => {
    setZoom(1);
    setRotation(0);
    setFlipX(false);
    setFlipY(false);
    setPan({ x: 0, y: 0 });
  };
  const turn = (degrees: number) => {
    // A single screen reflection reverses the rotation handedness.
    const direction = flipX !== flipY ? -degrees : degrees;
    setRotation((old) => (old + direction + 360) % 360);
    setPan({ x: 0, y: 0 });
  };
  useEffect(() => {
    reset();
    setNatural({ width: 0, height: 0 });
    setError(false);
  }, [src]);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const measure = () =>
      setView({ width: element.clientWidth, height: element.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", key, true);
    return () => {
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", key, true);
    };
  }, [onClose]);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      changeZoom(zoom * Math.exp(-Math.max(-100, Math.min(100, event.deltaY)) * 0.006));
    };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [zoom, natural, view, rotation, pan]);
  const finishDrag = (element: HTMLElement, id: number) => {
    if (element.hasPointerCapture(id)) element.releasePointerCapture(id);
    drag.current = null;
    setDragging(false);
  };
  const content = (
    <div
      className="modal-backdrop image-viewer-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="image-viewer"
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-label={`图片查看：${alt || "原文图片"}`}
        tabIndex={-1}
      >
        <header className="image-viewer-head">
          <strong title={alt}>
            <RichText text={alt || "原文图片"} />
          </strong>
          {onSource && (
            <button
              type="button"
              onClick={() => {
                onClose();
                onSource();
              }}
            >
              查看原页
            </button>
          )}
          <button type="button" aria-label="关闭图片查看" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="image-viewer-tools" role="toolbar" aria-label="图片显示调整">
          <button type="button" aria-label="向左旋转图片90度" onClick={() => turn(-90)}>
            ↶ 左转
          </button>
          <button type="button" aria-label="向右旋转图片90度" onClick={() => turn(90)}>
            ↷ 右转
          </button>
          <button
            type="button"
            aria-label="缩小图片"
            disabled={zoom <= 0.25}
            onClick={() => changeZoom(zoom / 1.25)}
          >
            −
          </button>
          <output aria-label="图片缩放比例">{Math.round(scale * 100)}%</output>
          <button
            type="button"
            aria-label="放大图片"
            disabled={zoom >= 8}
            onClick={() => changeZoom(zoom * 1.25)}
          >
            +
          </button>
          <button
            type="button"
            onClick={() => {
              setZoom(1);
              setPan({ x: 0, y: 0 });
            }}
          >
            适应窗口
          </button>
          <button type="button" onClick={reset}>
            复位
          </button>
          <details className="image-viewer-more">
            <summary>更多调整</summary>
            <div>
              <button
                type="button"
                aria-pressed={flipX}
                onClick={() => setFlipX((old) => !old)}
              >
                水平镜像
              </button>
              <button
                type="button"
                aria-pressed={flipY}
                onClick={() => setFlipY((old) => !old)}
              >
                垂直镜像
              </button>
              <small>仅调整当前显示方向</small>
            </div>
          </details>
        </div>
        <div
          ref={viewport}
          className={`image-viewer-viewport ${canDrag ? "can-drag" : ""} ${dragging ? "dragging" : ""}`}
          tabIndex={0}
          aria-label="图片区域，可放大后拖动或使用方向键平移"
          onKeyDown={(event) => {
            if (["+", "=", "-", "0"].includes(event.key)) {
              event.preventDefault();
              event.stopPropagation();
              if (event.key === "0") {
                setZoom(1);
                setPan({ x: 0, y: 0 });
              } else changeZoom(zoom * (event.key === "-" ? 0.8 : 1.25));
            } else if (canDrag && event.key.startsWith("Arrow")) {
              event.preventDefault();
              event.stopPropagation();
              const movement: Record<string, ImagePan> = {
                ArrowLeft: { x: 48, y: 0 },
                ArrowRight: { x: -48, y: 0 },
                ArrowUp: { x: 0, y: 48 },
                ArrowDown: { x: 0, y: -48 },
              };
              const delta = movement[event.key];
              if (delta)
                setPan(
                  imagePan(
                    { x: constrained.x + delta.x, y: constrained.y + delta.y },
                    natural,
                    view,
                    rotation,
                    scale,
                  ),
                );
            }
          }}
          onPointerDown={(event) => {
            if (!canDrag || !event.isPrimary || event.button !== 0) return;
            event.preventDefault();
            event.currentTarget.focus({ preventScroll: true });
            event.currentTarget.setPointerCapture(event.pointerId);
            drag.current = {
              id: event.pointerId,
              x: event.clientX,
              y: event.clientY,
              pan: constrained,
            };
            setDragging(true);
          }}
          onPointerMove={(event) => {
            const active = drag.current;
            if (!active || active.id !== event.pointerId) return;
            setPan(
              imagePan(
                {
                  x: active.pan.x + event.clientX - active.x,
                  y: active.pan.y + event.clientY - active.y,
                },
                natural,
                view,
                rotation,
                scale,
              ),
            );
          }}
          onPointerUp={(event) => finishDrag(event.currentTarget, event.pointerId)}
          onPointerCancel={(event) => finishDrag(event.currentTarget, event.pointerId)}
          onLostPointerCapture={() => {
            drag.current = null;
            setDragging(false);
          }}
        >
          {error ? (
            <div role="alert">
              <p>图片暂时无法显示，请查看原页或重试。</p>
              <button type="button" onClick={() => setError(false)}>
                重新加载图片
              </button>
            </div>
          ) : (
            <AssetImage
              src={src}
              loading="eager"
              onAssetError={() => setError(true)}
              alt={alt || "原文图片"}
              draggable={false}
              onLoad={(event) =>
                setNatural({
                  width: event.currentTarget.naturalWidth,
                  height: event.currentTarget.naturalHeight,
                })
              }
              onError={() => setError(true)}
              style={{
                width: natural.width || undefined,
                height: natural.height || undefined,
                visibility: natural.width ? "visible" : "hidden",
                transform: `translate(-50%, -50%) translate(${constrained.x}px, ${constrained.y}px) scaleX(${flipX ? -1 : 1}) scaleY(${flipY ? -1 : 1}) rotate(${rotation}deg) scale(${scale})`,
              }}
            />
          )}
        </div>
        <footer className="image-viewer-hint">
          滚轮或按钮缩放 · 放大后拖动 · Esc 关闭
        </footer>
      </div>
    </div>
  );
  return typeof document === "undefined" ? content : createPortal(content, document.body);
}
