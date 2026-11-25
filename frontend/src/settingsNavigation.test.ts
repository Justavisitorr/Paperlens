import { afterEach, describe, expect, it, vi } from "vitest";
import { go, href, parseRoute, SETTINGS_TARGET_EVENT } from "./router";
import { openSettings, revealSettingsSection, settingsHref } from "./settingsNavigation";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("settings destinations", () => {
  it("round-trips targeted and plain tabs while retaining the legacy route", () => {
    for (const destination of ["budget", "autoNotes", "experimental"] as const) {
      const hash = settingsHref(destination);
      expect(href(parseRoute(hash)!)).toBe(hash);
    }
    expect(parseRoute("#/settings")).toEqual({ page: "settings" });
    expect(parseRoute("#/settings/model")).toEqual({ page: "settings", tab: "model" });
    expect(parseRoute(settingsHref("experimental"))).toEqual({
      page: "settings",
      tab: "experimental",
      section: "experimental",
    });
    expect(parseRoute("#/settings/notes/tiered")).toEqual({
      page: "settings",
      tab: "notes",
    });
    expect(parseRoute("#/settings/unknown")).toEqual({ page: "settings" });
  });

  it("repeats targeting without pushing duplicate browser history entries", () => {
    vi.stubGlobal("location", { hash: settingsHref("autoNotes") });
    const dispatchEvent = vi.fn();
    const pushState = vi.fn();
    vi.stubGlobal("window", { dispatchEvent });
    vi.stubGlobal("history", { pushState });
    go({ page: "settings", tab: "notes", section: "auto_notes" });
    expect(pushState).not.toHaveBeenCalled();
    expect(dispatchEvent.mock.calls[0][0].type).toBe(SETTINGS_TARGET_EVENT);
  });

  it("leaves modified clicks to the browser", () => {
    const event = {
      button: 0,
      ctrlKey: true,
      metaKey: false,
      shiftKey: false,
      altKey: false,
      preventDefault: vi.fn(),
    };
    openSettings("budget", event);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });
});

function fixture({ delayed = false, editing = false, reduced = false } = {}) {
  vi.useFakeTimers();
  const classList = { add: vi.fn(), remove: vi.fn() };
  const control = { focus: vi.fn() };
  const target = {
    closest: vi.fn(() => null),
    scrollIntoView: vi.fn(),
    classList,
    querySelector: vi.fn(() => control),
  };
  let available = !delayed;
  const observer = { observe: vi.fn(), disconnect: vi.fn() };
  let onMutation = () => {};
  vi.stubGlobal(
    "MutationObserver",
    class {
      constructor(callback: () => void) {
        onMutation = callback;
      }
      observe = observer.observe;
      disconnect = observer.disconnect;
    },
  );
  const root = {
    ownerDocument: {
      documentElement: { dataset: { motion: reduced ? "reduced" : "full" } },
      defaultView: { matchMedia: () => ({ matches: false }) },
      activeElement: { matches: () => editing },
    },
    querySelector: vi.fn(() => (available ? target : null)),
  } as unknown as HTMLElement;
  return {
    root,
    target,
    control,
    observer,
    insert: () => {
      available = true;
      onMutation();
    },
  };
}

describe("settings section reveal", () => {
  it("waits for async schema, then locates an editable control and clears highlight", () => {
    const f = fixture({ delayed: true });
    const cleanup = revealSettingsSection(f.root, "auto_notes");
    expect(f.target.scrollIntoView).not.toHaveBeenCalled();
    expect(f.observer.observe).toHaveBeenCalled();
    f.insert();
    expect(f.target.scrollIntoView).toHaveBeenCalledWith({
      block: "start",
      behavior: "smooth",
    });
    expect(f.control.focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(f.observer.disconnect).toHaveBeenCalled();
    expect(f.target.classList.add).toHaveBeenCalledWith("settings-target-highlight");
    vi.advanceTimersByTime(2200);
    expect(f.target.classList.remove).toHaveBeenCalledWith("settings-target-highlight");
    cleanup();
  });

  it("does not steal focus from a field that the user has started editing", () => {
    const f = fixture({ editing: true });
    const cleanup = revealSettingsSection(f.root, "tiered");
    expect(f.target.scrollIntoView).toHaveBeenCalled();
    expect(f.control.focus).not.toHaveBeenCalled();
    cleanup();
  });

  it("avoids smooth scrolling for reduced motion and cleans up when hidden", () => {
    const f = fixture({ reduced: true });
    const cleanup = revealSettingsSection(f.root, "tiered");
    expect(f.target.scrollIntoView).toHaveBeenCalledWith({
      block: "start",
      behavior: "auto",
    });
    cleanup();
    expect(f.target.classList.remove).toHaveBeenCalledWith("settings-target-highlight");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("disconnects delayed positioning if the user leaves before settings arrive", () => {
    const f = fixture({ delayed: true });
    revealSettingsSection(f.root, "auto_notes")();
    expect(f.observer.disconnect).toHaveBeenCalledOnce();
    expect(f.target.scrollIntoView).not.toHaveBeenCalled();
  });
});
