import { go, href, type SettingsSection } from "./router";

export type SettingsDestination = "budget" | "autoNotes" | "catalog" | "experimental";
const destinations = {
  budget: { page: "settings", tab: "processing", section: "limits" },
  autoNotes: { page: "settings", tab: "notes", section: "auto_notes" },
  catalog: { page: "settings", tab: "catalog" },
  experimental: { page: "settings", tab: "experimental", section: "experimental" },
} as const;

export function settingsHref(destination: SettingsDestination) {
  return href(destinations[destination]);
}

/** Preserve native modified-link behavior (new tab/window). */
export function openSettings(
  destination: SettingsDestination,
  event?: {
    button: number;
    metaKey: boolean;
    ctrlKey: boolean;
    shiftKey: boolean;
    altKey: boolean;
    preventDefault: () => void;
  },
) {
  if (
    event &&
    (event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey)
  )
    return;
  event?.preventDefault();
  go(destinations[destination]);
}

/** Wait for async settings, then reveal once; navigation/unmount cancels it. */
export function revealSettingsSection(root: HTMLElement, section: SettingsSection) {
  let observer: MutationObserver | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let revealed: HTMLElement | undefined;
  const reveal = () => {
    const target = root.querySelector<HTMLElement>(
      `[data-settings-section="${section}"]`,
    );
    if (!target || target.closest("[hidden]")) return false;
    observer?.disconnect();
    revealed = target;
    const document = root.ownerDocument;
    const reduced =
      document.documentElement.dataset.motion === "reduced" ||
      Boolean(
        document.defaultView?.matchMedia("(prefers-reduced-motion: reduce)").matches,
      );
    target.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
    target.classList.add("settings-target-highlight");
    // A delayed response must not steal focus after the user has begun editing.
    if (
      !document.activeElement?.matches(
        'input, textarea, select, [contenteditable="true"]',
      )
    )
      target
        .querySelector<HTMLElement>(
          "input:not(:disabled), select:not(:disabled), textarea:not(:disabled)",
        )
        ?.focus({ preventScroll: true });
    timer = setTimeout(() => target.classList.remove("settings-target-highlight"), 2200);
    return true;
  };
  if (!reveal()) {
    observer = new MutationObserver(reveal);
    observer.observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["hidden"],
    });
  }
  return () => {
    observer?.disconnect();
    if (timer) clearTimeout(timer);
    revealed?.classList.remove("settings-target-highlight");
  };
}
