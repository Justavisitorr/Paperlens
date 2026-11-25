import { afterEach, describe, expect, it, vi } from "vitest";
import { backgroundShortcutBlocked, sourceShortcutTarget } from "./dialog";

afterEach(() => vi.unstubAllGlobals());
const event = (patch: Partial<KeyboardEvent> = {}) =>
  ({
    defaultPrevented: false,
    isComposing: false,
    ...patch,
  }) as KeyboardEvent;

describe("reader keyboard boundaries", () => {
  it("blocks background search, mode switching and paragraph keys while a modal owns focus", () => {
    const querySelector = vi.fn((_selector: string) => ({ role: "dialog" }));
    vi.stubGlobal("document", { querySelector });
    for (const key of ["1", "2", "j", "k", "Escape"]) {
      expect(backgroundShortcutBlocked(event({ key }))).toBe(true);
    }
    expect(backgroundShortcutBlocked(event({ key: "k", ctrlKey: true }))).toBe(true);
    expect(querySelector.mock.calls[0][0]).toContain('[aria-modal="true"]');
  });
  it("leaves consumed keys and IME composition alone, and permits ordinary reading keys", () => {
    vi.stubGlobal("document", { querySelector: () => null });
    expect(backgroundShortcutBlocked(event({ defaultPrevented: true }))).toBe(true);
    expect(backgroundShortcutBlocked(event({ isComposing: true }))).toBe(true);
    expect(backgroundShortcutBlocked(event({ key: "j" }))).toBe(false);
  });
  it("recognizes PDF controls as a separate keyboard scope from the prose", () => {
    const closest = vi.fn((_selector: string) => ({ className: "source-inspector" }));
    expect(
      sourceShortcutTarget(event({ target: { closest } as unknown as EventTarget })),
    ).toBe(true);
    expect(closest.mock.calls[0][0]).toContain(".source-inspector");
    expect(
      sourceShortcutTarget(
        event({ target: { closest: () => null } as unknown as EventTarget }),
      ),
    ).toBe(false);
  });
});
