import { useEffect, useRef, useState, type ImgHTMLAttributes } from "react";
import { embeddedImage, retainImage } from "./reportAssets";

/** Load online image bytes only near the viewport, through account-bound fetch. */
export function AssetImage({
  src = "",
  loading = "lazy",
  onAssetError,
  retryable = true,
  ...props
}: Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> & {
  src?: string;
  onAssetError?: (message: string) => void;
  /** Disable within a button that already opens a retryable image viewer. */
  retryable?: boolean;
}) {
  const element = useRef<HTMLImageElement>(null);
  const failure = useRef(onAssetError);
  failure.current = onAssetError;
  const inline = embeddedImage(src);
  const [visible, setVisible] = useState(loading === "eager");
  const [resolved, setResolved] = useState<{ source: string; url: string } | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (inline || loading === "eager") {
      setVisible(true);
      return;
    }
    if (!element.current) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "300px" },
    );
    observer.observe(element.current);
    return () => observer.disconnect();
  }, [inline, loading, src]);
  useEffect(() => {
    setError("");
    if (inline || !visible || !src) return;
    let alive = true;
    let retained: ReturnType<typeof retainImage> | undefined;
    const failed = (reason: unknown) => {
      if (!alive) return;
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message);
      failure.current?.(message);
    };
    try {
      retained = retainImage(src);
      retained.url.then((url) => alive && setResolved({ source: src, url }), failed);
    } catch (reason) {
      failed(reason);
    }
    return () => {
      alive = false;
      retained?.release();
    };
  }, [src, inline, visible, attempt]);
  return (
    <>
      <img
        {...props}
        ref={element}
        src={inline ? src : resolved?.source === src ? resolved.url : undefined}
        loading={loading}
        onError={(event) => {
          const message = "图片暂时无法显示，请重试或查看原页。";
          setError(message);
          failure.current?.(message);
          props.onError?.(event);
        }}
        aria-busy={
          (!inline && visible && resolved?.source !== src && !error) || undefined
        }
        title={error || props.title}
        data-asset-error={error || undefined}
      />
      {error && !onAssetError && (
        <span role="alert" style={{ display: "block", fontSize: ".85em" }}>
          {error}{" "}
          {retryable && (
            <button type="button" onClick={() => setAttempt((value) => value + 1)}>
              重新加载图片
            </button>
          )}
        </span>
      )}
    </>
  );
}
