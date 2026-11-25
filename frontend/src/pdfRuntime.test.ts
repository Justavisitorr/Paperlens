import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const runtime = vi.hoisted(() => ({
  tasks: [] as {
    destroy: ReturnType<typeof vi.fn>;
    promise: Promise<{ numPages: number }>;
  }[],
  getDocument: vi.fn(),
}));
vi.mock("pdfjs-dist", () => ({
  PDFWorker: { create: vi.fn(() => ({})) },
  getDocument: runtime.getDocument,
  PDFDataRangeTransport: class {
    onDataRange = vi.fn();
    constructor(
      public length: number,
      public initialData: Uint8Array,
      public progressiveDone: boolean,
    ) {}
  },
}));

import { sourceDocument } from "./pdfRuntime";
import { PDF_RANGE_BYTES } from "./pdfRanges";

beforeEach(() => {
  runtime.tasks.length = 0;
  runtime.getDocument.mockReset().mockImplementation(() => {
    const task = {
      promise: Promise.resolve({ numPages: 12 }),
      destroy: vi.fn(async () => {}),
    };
    runtime.tasks.push(task);
    return task;
  });
  vi.stubGlobal("Worker", class {});
});
afterEach(() => vi.unstubAllGlobals());

const report = (id: string) => ({
  source_pdf: { data_url: `/api/library/${id}/pdf`, sha256: id, byte_size: 200000 },
});
const part = (begin: number, end: number, total = 200000) =>
  new Response(new Uint8Array(end - begin), {
    status: 206,
    headers: {
      "content-type": "application/pdf",
      "content-range": `bytes ${begin}-${end - 1}/${total}`,
    },
  });

describe("lazy PDF document transport", () => {
  it("shares one document and fetches only the initial chunk plus explicitly requested ranges", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(part(0, PDF_RANGE_BYTES))
      .mockResolvedValueOnce(part(131072, 196608));
    vi.stubGlobal("fetch", fetch);
    expect(fetch).not.toHaveBeenCalled();
    const first = sourceDocument(report("range")),
      second = sourceDocument(report("range"));
    expect(first.document).toBe(second.document);
    await first.document;
    expect(fetch).toHaveBeenCalledTimes(1);
    const options = runtime.getDocument.mock.calls[0][0];
    expect(options).toMatchObject({
      disableAutoFetch: true,
      disableStream: true,
      rangeChunkSize: PDF_RANGE_BYTES,
    });
    expect(options).not.toHaveProperty("url");
    expect(options.range.initialData).toHaveLength(PDF_RANGE_BYTES);
    expect(options.range.length).toBe(200000);
    options.range.requestDataRange(131072, 196608);
    await vi.waitFor(() => expect(options.range.onDataRange).toHaveBeenCalledTimes(1));
    expect(fetch.mock.calls[1][1]).toMatchObject({
      headers: { Range: "bytes=131072-196607" },
      credentials: "same-origin",
    });
    first.release();
    expect(runtime.tasks[0].destroy).not.toHaveBeenCalled();
    second.release();
    expect(runtime.tasks[0].destroy).toHaveBeenCalledTimes(1);
  });

  it("reports a later range failure and permits an explicit retry while another old pane remains mounted", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(part(0, PDF_RANGE_BYTES))
      .mockResolvedValueOnce(new Response("expired workspace", { status: 409 }))
      .mockResolvedValueOnce(part(0, PDF_RANGE_BYTES));
    vi.stubGlobal("fetch", fetch);
    const failed = sourceDocument(report("retry"));
    const onError = vi.fn();
    const unsubscribe = failed.onError(onError);
    await failed.document;
    runtime.getDocument.mock.calls[0][0].range.requestDataRange(131072, 196608);
    await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    expect(onError.mock.calls[0][0].message).toContain("409");
    expect(runtime.tasks[0].destroy).toHaveBeenCalledTimes(1);
    const retry = sourceDocument(report("retry"));
    expect(retry.document).not.toBe(failed.document);
    await retry.document;
    unsubscribe();
    failed.release();
    const other = sourceDocument(report("retry"));
    expect(other.document).toBe(retry.document);
    expect(fetch).toHaveBeenCalledTimes(3);
    retry.release();
    other.release();
  });

  it("reads an embedded PDF without any network access", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const source = sourceDocument({
      source_pdf: {
        data_url: "data:application/pdf;base64,JVBERi0xLjQ=",
        sha256: "offline",
      },
    });
    await source.document;
    expect(new TextDecoder().decode(runtime.getDocument.mock.calls[0][0].data)).toBe(
      "%PDF-1.4",
    );
    expect(fetch).not.toHaveBeenCalled();
    source.release();
  });

  it("cancels an initial range if the source pane closes before PDF.js is created", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_path: string, init: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    const source = sourceDocument(report("closed"));
    const rejected = expect(source.document).rejects.toThrow("Aborted");
    source.release();
    await rejected;
    expect(runtime.getDocument).not.toHaveBeenCalled();
  });
});
