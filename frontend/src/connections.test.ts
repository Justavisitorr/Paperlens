import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
import {
  jobAction,
  saveModelAccount,
  saveModelRouting,
  refreshAccountModels,
  type ModelRouting,
} from "./api";
import { UploadPage } from "./UploadPage";

function mockResponse(body: unknown) {
  const fetchMock = vi.fn(
    async (_path: string, _init: RequestInit) =>
      new Response(JSON.stringify(body), {
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
afterEach(() => vi.unstubAllGlobals());

describe("model connection requests", () => {
  it("saves a second connection without storing a key in browser storage", async () => {
    const storage = { setItem: vi.fn() };
    vi.stubGlobal("localStorage", storage);
    const fetchMock = mockResponse({ account: { id: "second" } });
    await saveModelAccount({
      provider_id: "deepseek",
      name: "个人账户",
      api_key: "test-only-key",
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/connections/accounts");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toMatchObject({
      api_key: "test-only-key",
      name: "个人账户",
    });
    expect(storage.setItem).not.toHaveBeenCalled();
    await saveModelAccount(
      { provider_id: "deepseek", name: "改名", api_key: "" },
      "second",
    );
    expect(fetchMock.mock.calls[1][0]).toBe("/api/connections/accounts/second");
    expect(fetchMock.mock.calls[1][1].method).toBe("PATCH");
    expect(JSON.parse(fetchMock.mock.calls[1][1].body as string).api_key).toBe("");
  });
  it("sends per-role assignments and explicit vision confirmation", async () => {
    const routing: ModelRouting = {
      main: { account_id: "one", model: "reader" },
      reading: null,
      verification: { account_id: "two", model: "checker" },
      vision: { account_id: "two", model: "vision", vision_confirmed: true },
      chat: null,
      conversation: null,
    };
    const fetchMock = mockResponse({ routing });
    expect(await saveModelRouting(routing)).toEqual({ routing });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({ routing });
    expect(fetchMock.mock.calls[0][1].method).toBe("PUT");
  });
  it("explicitly refreshes the selected account's provider models", async () => {
    const models = { items: [{ id: "custom-model", vision: "unknown" }], status: "live" };
    const fetchMock = mockResponse(models);
    expect(await refreshAccountModels("account-one")).toEqual(models);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/connections/accounts/account-one/models/refresh",
    );
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
  });
});

describe("job acceleration", () => {
  it("sends confirmation only for acceleration, keeping other controls compatible", async () => {
    const fetchMock = mockResponse({
      id: "job",
      acceleration: { enabled: true, concurrency: 3 },
    });
    await jobAction("job", "accelerate");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({
      confirmed: true,
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs/job/accelerate");
    await jobAction("job", "pause");
    expect(fetchMock.mock.calls[1][1].body).toBeUndefined();
  });
  it("places a visible acceleration button before pause and cancel", () => {
    const html = renderToStaticMarkup(
      createElement(UploadPage, {
        staged: [],
        jobs: [
          {
            id: "job",
            source_name: "paper.pdf",
            status: "running",
            stage: "analysis",
            message: "读取论文",
            actions: ["pause", "cancel", "accelerate"],
          },
        ],
        jobsSource: "native",
        sessionJobs: new Set<string>(),
        busy: false,
        error: "",
        canUpload: true,
        onPick: () => {},
        onDropFiles: () => {},
        onUnstage: () => {},
        onStart: () => {},
        onAction: () => {},
      }),
    );
    expect(html).toContain('aria-label="加速 paper.pdf"');
    expect(html.indexOf('aria-label="加速')).toBeLessThan(
      html.indexOf('aria-label="暂停'),
    );
    expect(html.indexOf('aria-label="暂停')).toBeLessThan(
      html.indexOf('aria-label="取消'),
    );
  });
});
