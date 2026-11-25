import { useEffect, type RefObject } from "react";

const focusable =
  "a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex='-1'])";

/** Modal keys belong to the top layer; background readers must never consume them. */
export function backgroundShortcutBlocked(event: KeyboardEvent): boolean {
  return (
    event.defaultPrevented ||
    event.isComposing ||
    !!document.querySelector('[role="dialog"][aria-modal="true"], .modal-backdrop')
  );
}

export function sourceShortcutTarget(event: KeyboardEvent): boolean {
  const target = event.target as Element | null;
  return !!target?.closest?.(".source-inspector,.pdf-source,.pdf-source-scroll");
}

/**
 * Modal focus: move focus inside on open, keep Tab cycling within the
 * dialog, and hand focus back to where it was on close.
 */
export function useFocusTrap(ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const box = ref.current;
    if (!box) return;
    const previous = document.activeElement as HTMLElement | null;
    if (!box.contains(document.activeElement)) box.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const items = [...box.querySelectorAll<HTMLElement>(focusable)].filter(
        (el) => el.offsetParent !== null,
      );
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === box)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !box.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      if (previous?.isConnected) previous.focus({ preventScroll: true });
    };
  }, [ref]);
}
