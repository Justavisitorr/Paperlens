import { afterEach, describe, expect, it, vi } from "vitest";
import { readPdfRange } from "./pdfRanges";

const path = "/api/library/paper-a/pdf";
const signal = () => new AbortController().signal;
afterEach(() => vi.unstubAllGlobals());

describe("authenticated PDF byte ranges", () => {
  it("reads a bounded range without requiring Content-Length", async () => {
    const fetch = vi.fn(
      async () =>
        new Response(new Uint8Array([4, 5, 6, 7]), {
          status: 206,
          headers: { "Content-Type": "application/pdf", "Content-Range": "bytes 4-7/20" },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const abort = signal();
    const result = await readPdfRange(path, 4, 8, abort, 20);
    expect(result).toEqual({
      bytes: new Uint8Array([4, 5, 6, 7]),
      length: 20,
      complete: false,
    });
    expect(fetch).toHaveBeenCalledWith(path, {
      signal: abort,
      credentials: "same-origin",
      headers: { Range: "bytes=4-7" },
    });
  });

  it("accepts the final shorter range and a legacy full response", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          new Response(new Uint8Array([8, 9]), {
            status: 206,
            headers: {
              "Content-Type": "application/pdf",
              "Content-Range": "bytes 8-9/10",
            },
          }),
        )
        .mockResolvedValueOnce(
          new Response(new Uint8Array([0, 1, 2]), {
            headers: { "Content-Type": "application/pdf" },
          }),
        ),
    );
    expect((await readPdfRange(path, 8, 16, signal(), 10)).bytes).toHaveLength(2);
    expect(await readPdfRange(path, 0, 8, signal(), 3)).toEqual({
      bytes: new Uint8Array([0, 1, 2]),
      length: 3,
      complete: true,
    });
  });

  it.each([
    [206, "bytes 5-8/20", "application/pdf", 4, 20],
    [206, "bytes 4-7/20", "application/pdf", 3, 20],
    [206, "bytes 4-7/21", "application/pdf", 4, 20],
    [206, "", "application/pdf", 4, 20],
    [206, "bytes 4-7/20", "text/html", 4, 20],
    [200, "", "application/pdf", 4, 20],
    [409, "", "text/plain", 4, 20],
  ])(
    "rejects mismatched/authentication/invalid responses (%s, %s, %s)",
    async (status, range, type, size, total) => {
      const fetch = vi.fn(
        async () =>
          new Response(new Uint8Array(size), {
            status,
            headers: { "Content-Type": type, "Content-Range": range },
          }),
      );
      vi.stubGlobal("fetch", fetch);
      await expect(readPdfRange(path, 4, 8, signal(), total)).rejects.toThrow();
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  it("rejects foreign and malformed resource URLs before making a request", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    for (const source of [
      "//evil.example/api/library/paper-a/pdf",
      "/api/library/../pdf",
      "javascript:alert(1)",
    ])
      await expect(readPdfRange(source, 0, 8, signal())).rejects.toThrow("地址无效");
    await expect(readPdfRange(path, -1, 2, signal())).rejects.toThrow("范围无效");
    expect(fetch).not.toHaveBeenCalled();
  });
});
