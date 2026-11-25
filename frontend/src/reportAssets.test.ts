import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hasExternalAssets, resourcePath, retainImage } from "./reportAssets";
import type { DashboardReport } from "./types";

const asset = `/api/library/paper-a/assets/${"a".repeat(64)}`;
beforeEach(() => vi.stubGlobal("location", { origin: "https://paperlens.example" }));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("scoped report assets", () => {
  it("allows only the same-origin paper endpoints", () => {
    expect(resourcePath(asset, "image")).toBe(asset);
    expect(resourcePath(`https://paperlens.example${asset}`, "image")).toBe(asset);
    expect(resourcePath("/api/library/paper-a/pdf", "pdf")).toBe(
      "/api/library/paper-a/pdf",
    );
    expect(resourcePath("/api/library/paper-a/thumbnail", "image")).toBe(
      "/api/library/paper-a/thumbnail",
    );
    for (const source of [
      `https://another.example${asset}`,
      `//paperlens.example${asset}`,
      `${asset}?identity=other`,
      "/api/library/../assets/a",
      "/api/library/a/pdf",
      "blob:untrusted",
      "javascript:alert(1)",
    ])
      expect(resourcePath(source, "image")).toBeNull();
  });

  it("shares overlapping fetches and releases the blob only after the final consumer", async () => {
    const fetch = vi.fn(
      async (_path: string, _init: RequestInit) =>
        new Response(new Blob(["image"], { type: "image/png" })),
    );
    const created = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:figure");
    const revoked = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.stubGlobal("fetch", fetch);
    const first = retainImage(asset),
      second = retainImage(asset);
    expect(await first.url).toBe("blob:figure");
    expect(await second.url).toBe("blob:figure");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(created).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1]).toMatchObject({ credentials: "same-origin" });
    first.release();
    first.release();
    expect(revoked).not.toHaveBeenCalled();
    second.release();
    expect(revoked).toHaveBeenCalledExactlyOnceWith("blob:figure");
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true);
  });

  it("allows an explicit retry after failure and never accepts a login page as an image", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response("sign in", { headers: { "content-type": "text/html" } }),
      )
      .mockResolvedValueOnce(new Response(new Blob(["image"], { type: "image/png" })));
    vi.stubGlobal("fetch", fetch);
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:retry");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const failed = retainImage(asset);
    await expect(failed.url).rejects.toThrow("有效图片");
    const retry = retainImage(asset);
    failed.release();
    expect(await retry.url).toBe("blob:retry");
    expect(fetch).toHaveBeenCalledTimes(2);
    retry.release();
  });

  it("aborts an unfinished request when its only consumer leaves", async () => {
    const fetch = vi.fn(
      (_path: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const image = retainImage(asset);
    const rejected = expect(image.url).rejects.toThrow("Aborted");
    image.release();
    await rejected;
  });

  it("detects remote binary resources in every report slot before offline export", () => {
    const inline = "data:image/png;base64,aW1hZ2U=";
    const base = {
      source_pdf: { data_url: "data:application/pdf;base64,UERG" },
      previews: [{ data_url: inline }],
      reading: { items: [{ asset: inline }], references: [], information_items: {} },
    } as unknown as DashboardReport;
    expect(hasExternalAssets(base)).toBe(false);
    expect(
      hasExternalAssets({
        ...base,
        source_pdf: { data_url: "/api/library/paper-a/pdf", sha256: "hash" },
      }),
    ).toBe(true);
    expect(
      hasExternalAssets({ ...base, previews: [{ data_url: asset }] } as DashboardReport),
    ).toBe(true);
    for (const category of ["items", "references"])
      expect(
        hasExternalAssets({
          ...base,
          reading: { ...base.reading, [category]: [{ asset }] },
        } as DashboardReport),
      ).toBe(true);
    expect(
      hasExternalAssets({
        ...base,
        reading: { ...base.reading, information_items: { authors: [{ asset }] } },
      } as unknown as DashboardReport),
    ).toBe(true);
  });
});
