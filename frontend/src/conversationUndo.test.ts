import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "file:" };
});
import {
  blankConversation,
  configureChat,
  ensureSession,
  getSession,
  removeConversation,
  restoreConversation,
} from "./chat";
import { deletedConversations, forgetDeletedConversation } from "./conversationUndo";

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: vi.fn((key: string, value: string) => {
      data.set(key, value);
    }),
    removeItem: (key: string) => {
      data.delete(key);
    },
  };
}

describe("conversation deletion recovery", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", memoryStorage());
    vi.stubGlobal("sessionStorage", memoryStorage());
    configureChat({ remote: false });
    for (const doc of deletedConversations()) forgetDeletedConversation(doc.id);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps separate recovery entries for consecutive deletes until each restore succeeds", async () => {
    const a = { ...blankConversation(), title: "A" };
    const b = { ...blankConversation(), title: "B" };
    ensureSession(a.id, () => a, true);
    ensureSession(b.id, () => b, true);
    await removeConversation(a.id);
    await removeConversation(b.id);
    expect(deletedConversations().map((doc) => doc.title)).toEqual(["B", "A"]);
    expect(
      JSON.parse(sessionStorage.getItem("paperlens.deleted-conversations")!).length,
    ).toBe(2);
    await restoreConversation(a);
    expect(deletedConversations().map((doc) => doc.id)).toEqual([b.id]);
    expect(getSession(a.id)?.doc.title).toBe("A");
    expect(
      JSON.parse(localStorage.getItem("paperlens.chats")!).map(
        (doc: { id: string }) => doc.id,
      ),
    ).toContain(a.id);
    await restoreConversation(b);
    expect(deletedConversations()).toEqual([]);
  });

  it("retains the recovery entry and does not fake success after a failed restore", async () => {
    const doc = blankConversation();
    ensureSession(doc.id, () => doc, true);
    await removeConversation(doc.id);
    vi.mocked(localStorage.setItem).mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    await expect(restoreConversation(doc)).rejects.toThrow("storage unavailable");
    expect(deletedConversations().map((item) => item.id)).toContain(doc.id);
    expect(getSession(doc.id)).toBeUndefined();
  });

  it("does not discard a live conversation when its deletion fails", async () => {
    const doc = blankConversation();
    ensureSession(doc.id, () => doc, true);
    vi.mocked(localStorage.setItem).mockImplementation(() => {
      throw new Error("delete unavailable");
    });
    await expect(removeConversation(doc.id)).rejects.toThrow("delete unavailable");
    expect(getSession(doc.id)?.doc.id).toBe(doc.id);
    expect(deletedConversations().some((item) => item.id === doc.id)).toBe(false);
  });
});
