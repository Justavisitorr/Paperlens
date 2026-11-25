import { afterEach, describe, expect, it, vi } from "vitest";
import { confirmInputBudget, type InputBudget } from "./inputBudget";
vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
import { jobAction } from "./api";
import {
  blankConversation,
  configureChat,
  discard,
  ensureSession,
  getSession,
  send,
} from "./chat";

const pending: InputBudget = {
  limit_chars: 18000,
  required_chars: 23000,
  suggested_chars: 26000,
  maximum_chars: 1000000,
  stage: "自动笔记",
};
afterEach(() => vi.unstubAllGlobals());

describe("one-attempt input budget approval", () => {
  it.each([false, true])(
    "requires approval before resending a streamed question: %s",
    async (accepted) => {
      const key = `quick:budget-${accepted}`;
      const confirm = vi.fn(() => accepted);
      vi.stubGlobal("window", { confirm });
      const fetch = vi
        .fn()
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({ detail: "输入长度超限", input_budget: pending }),
            { status: 422, headers: { "Content-Type": "application/json" } },
          ),
        )
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              type: "done",
              reply: { answer: "saved", quotes: [], grounded: false },
            }) + "\n",
            { headers: { "Content-Type": "application/x-ndjson" } },
          ),
        );
      vi.stubGlobal("fetch", fetch);
      configureChat({
        multi: true,
        stream: true,
        routing: false,
        library: new Set(["a"]),
      });
      try {
        ensureSession(
          key,
          () => blankConversation([{ kind: "paper", paper_id: "a", title: "A" }]),
          false,
        );
        send(key, "Explain");
        await vi.waitFor(() => expect(getSession(key)?.pending).toBeNull());
        expect(confirm).toHaveBeenCalledOnce();
        expect(fetch).toHaveBeenCalledTimes(accepted ? 2 : 1);
        expect(JSON.parse(fetch.mock.calls[0][1].body).input_budget).toBeUndefined();
        if (accepted)
          expect(JSON.parse(fetch.mock.calls[1][1].body).input_budget).toEqual({
            confirmed: true,
            limit_chars: 26000,
          });
      } finally {
        discard(key);
        configureChat({ multi: false, stream: false, routing: false, library: null });
      }
    },
  );
  it("does not create an approval when cancelled", () => {
    const confirm = vi.fn((_message: string) => false);
    vi.stubGlobal("window", { confirm });
    expect(confirmInputBudget(pending, "Paper")).toBeUndefined();
    expect(confirm.mock.calls[0][0]).toContain("18,000");
    expect(confirm.mock.calls[0][0]).toContain("26,000");
    expect(confirm.mock.calls[0][0]).toContain("暂停");
  });
  it("sends an explicit approval only with this retry", async () => {
    vi.stubGlobal("window", { confirm: vi.fn(() => true) });
    const fetch = vi.fn(
      async (_path: string, _init: RequestInit) =>
        new Response("{}", { headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetch);
    await jobAction("job", "retry", confirmInputBudget(pending, "Paper"));
    expect(JSON.parse(fetch.mock.calls[0][1].body as string)).toEqual({
      input_budget: { confirmed: true, limit_chars: 26000 },
    });
    await jobAction("job", "retry");
    expect(fetch.mock.calls[1][1].body).toBeUndefined();
  });
  it("does not offer an increase beyond the supported ceiling", () => {
    const alert = vi.fn(),
      confirm = vi.fn();
    vi.stubGlobal("window", { alert, confirm });
    expect(
      confirmInputBudget({ ...pending, suggested_chars: null }, "Paper"),
    ).toBeUndefined();
    expect(alert).toHaveBeenCalledOnce();
    expect(confirm).not.toHaveBeenCalled();
  });
});
