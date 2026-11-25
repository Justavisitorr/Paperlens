import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
import { estimateProcessing, submitJob, startImport } from "./api";

const features = { chars_per_page: 2100, math_density: 0.1, source: "pdf" };
const response = (pages: number, pagesAssumed = false) =>
  new Response(
    JSON.stringify({
      pages,
      pages_assumed: pagesAssumed,
      document_features: features,
      profiles: [],
    }),
    { headers: { "content-type": "application/json" } },
  );

afterEach(() => vi.unstubAllGlobals());

describe("estimate requests", () => {
  it("recalculates with current settings using cached PDF metadata without reuploading", async () => {
    const file = new File(["test"], "paper.pdf", { type: "application/pdf" });
    const fetchMock = vi.fn(async (_path: string, _init: RequestInit) => response(8));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    await estimateProcessing(file, controller.signal);
    await estimateProcessing(file);
    const first = fetchMock.mock.calls[0][1];
    const second = fetchMock.mock.calls[1][1];
    expect(first.signal).toBe(controller.signal);
    expect((first.body as FormData).get("file")).toBeInstanceOf(File);
    expect((second.body as FormData).has("file")).toBe(false);
    expect((second.body as FormData).get("pages")).toBe("8");
    expect(
      JSON.parse((second.body as FormData).get("document_features") as string),
    ).toEqual(features);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not cache an assumed page count as the real PDF", async () => {
    const file = new File(["test"], "unknown.pdf");
    const fetchMock = vi.fn(async (_path: string, _init: RequestInit) =>
      response(8, true),
    );
    vi.stubGlobal("fetch", fetchMock);
    await estimateProcessing(file);
    await estimateProcessing(file);
    expect((fetchMock.mock.calls[1][1].body as FormData).has("file")).toBe(true);
  });

  it("recalculates accelerated estimates using cached document metadata", async () => {
    const file = new File(["test"], "accelerated.pdf");
    const fetchMock = vi.fn(async (_path: string, _init: RequestInit) => response(12));
    vi.stubGlobal("fetch", fetchMock);
    await estimateProcessing(file, undefined, false);
    await estimateProcessing(file, undefined, true);
    expect((fetchMock.mock.calls[0][1].body as FormData).get("accelerated")).toBe(
      "false",
    );
    const body = fetchMock.mock.calls[1][1].body as FormData;
    expect(body.get("accelerated")).toBe("true");
    expect(body.get("pages")).toBe("12");
    expect(body.has("file")).toBe(false);
  });

  it("sends acceleration with each upload and imported paper", async () => {
    const fetchMock = vi.fn(
      async (_path: string, _init: RequestInit) =>
        new Response(JSON.stringify({ id: "job-id" }), {
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await submitJob(new File(["pdf"], "first.pdf"), "economy", false, true);
    await submitJob(new File(["pdf"], "second.pdf"), "economy", false, true);
    await startImport("candidate", "detailed", false, true);
    for (const call of fetchMock.mock.calls.slice(0, 2)) {
      expect((call[1].body as FormData).get("accelerated")).toBe("true");
      expect((call[1].body as FormData).get("profile")).toBe("economy");
    }
    expect(JSON.parse(fetchMock.mock.calls[2][1].body as string)).toMatchObject({
      accelerated: true,
      profile: "detailed",
    });
  });

  it("forwards cancellation to the pending estimate request", async () => {
    const fetchMock = vi.fn(
      (_path: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal!.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const pending = estimateProcessing(undefined, controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
});
