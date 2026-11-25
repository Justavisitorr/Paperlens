import { useEffect, useRef, useState, type ComponentProps, type ReactNode } from "react";

import { flushSync } from "react-dom";

// Native browser find/print must include paragraphs outside the viewport.
function revealAll() {
  flushSync(() => {
    for (const pool of pools.values())
      for (const reveal of [...pool.callbacks.values()]) reveal();
  });
}
function nativeFind(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") revealAll();
}

type ObserverPool = {
  observer: IntersectionObserver;
  callbacks: Map<Element, () => void>;
};
const pools = new Map<Element | null, ObserverPool>();
function observe(element: Element, reveal: () => void) {
  const root = element.closest(".reader-main");
  let pool = pools.get(root);
  if (!pool) {
    const callbacks = new Map<Element, () => void>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries)
          if (entry.isIntersecting) callbacks.get(entry.target)?.();
      },
      { root, rootMargin: "900px 0px" },
    );
    if (!pools.size) {
      window.addEventListener("beforeprint", revealAll);
      window.addEventListener("keydown", nativeFind);
    }
    pool = { callbacks, observer };
    pools.set(root, pool);
  }
  pool.callbacks.set(element, reveal);
  pool.observer.observe(element);
  return () => {
    pool!.observer.unobserve(element);
    pool!.callbacks.delete(element);
    if (!pool!.callbacks.size) {
      pool!.observer.disconnect();
      pools.delete(root);
      if (!pools.size) {
        window.removeEventListener("beforeprint", revealAll);
        window.removeEventListener("keydown", nativeFind);
      }
    }
  };
}

/** Keep every anchor; only materialise expensive prose/formulas near the viewport.
 * Once read, content remains mounted so selections and annotations stay stable. */
export function DeferredReadingSection({
  eager,
  estimatedHeight,
  placeholderAnchors,
  render,
  ...props
}: {
  eager: boolean;
  estimatedHeight: number;
  placeholderAnchors: ReactNode;
  render: () => ReactNode;
} & Omit<ComponentProps<"section">, "children">) {
  const [loaded, setLoaded] = useState(eager);
  const element = useRef<HTMLElement>(null);
  useEffect(() => {
    if (loaded || !element.current) return;
    if (typeof IntersectionObserver === "undefined") {
      setLoaded(true);
      return;
    }
    return observe(element.current, () => setLoaded(true));
  }, [loaded]);
  return (
    <section
      {...props}
      ref={element}
      data-deferred={loaded ? undefined : "true"}
      onFocusCapture={() => setLoaded(true)}
    >
      {loaded ? (
        render()
      ) : (
        <>
          {placeholderAnchors}
          <button
            className="reading-placeholder"
            style={{ minHeight: estimatedHeight }}
            onClick={() => setLoaded(true)}
          >
            载入这段正文
          </button>
        </>
      )}
    </section>
  );
}
