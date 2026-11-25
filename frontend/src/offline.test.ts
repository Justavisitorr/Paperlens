import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  // api.ts reads the page protocol when it loads.
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});

import { loadCompleteReport, loadReport, offlineHtml } from "./api";

const TEMPLATE = [
  "<!doctype html><html><head><script>const a = '</body>';</script></head><body>",
  '<script id="paperlens-data" type="application/json">\nnull\n</script>',
  '<div id="root"></div></body></html>',
].join("");
const NOTICES =
  '<script id="paperlens-open-source-notices" type="application/json">"MIT"</script>';
/** Just the fields the library metadata reads, plus a hostile title. */
const report = {
  analysis: { title: "论文 </script><script>alert(1)</script>", nodes: [] },
  document: { pages: [] },
  evidence: [],
  run: {},
};

function serve(routes: Record<string, [string, string]>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      const [body, type] = routes[path] ?? ["", "text/plain"];
      return new Response(body, {
        status: path in routes ? 200 : 404,
        headers: { "content-type": type },
      });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("offlineHtml", () => {
  it("downloads full binary data for HTML and JSON exports after a lightweight reading session", async () => {
    const image = "data:image/png;base64,aW1hZ2U=",
      pdf = "data:application/pdf;base64,UERG";
    const asset = `/api/library/lazy/assets/${"a".repeat(64)}`;
    const light = {
      ...report,
      source_pdf: { data_url: "/api/library/lazy/pdf" },
      previews: [{ data_url: asset }],
      reading: { items: [{ asset }], references: [], information_items: {} },
    };
    const full = {
      ...report,
      source_pdf: { data_url: pdf },
      previews: [{ data_url: image }],
      reading: { items: [{ asset: image }], references: [], information_items: {} },
    };
    serve({
      "/": [TEMPLATE, "text/html"],
      "/api/library/lazy/report?view=web": [JSON.stringify(light), "application/json"],
      "/api/library/lazy/report": [JSON.stringify(full), "application/json"],
    });
    const paper = { id: "lazy" };
    expect(await loadReport(paper)).toEqual(light);
    expect(vi.mocked(fetch).mock.calls.map(([url]) => url)).toEqual([
      "/api/library/lazy/report?view=web",
    ]);
    const [html, complete] = await Promise.all([
      offlineHtml(paper),
      loadCompleteReport(paper),
    ]);
    expect(complete).toEqual(full);
    expect(html).toContain(pdf);
    expect(html).toContain(image);
    expect(html).not.toContain("/api/library/lazy");
    expect(JSON.stringify(complete)).not.toContain("/api/library/lazy");
    expect(
      vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/library/lazy/report"),
    ).toHaveLength(1);
    // Export must not replace the small reading cache with a binary-filled report.
    expect(await loadReport(paper)).toEqual(light);
    await loadCompleteReport(paper);
    expect(
      vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/library/lazy/report"),
    ).toHaveLength(2);
  });

  it("refuses to label unresolved assets as a complete offline export", async () => {
    serve({
      "/": [TEMPLATE, "text/html"],
      "/api/library/incomplete/report": [
        JSON.stringify({
          ...report,
          source_pdf: { data_url: "/api/library/incomplete/pdf" },
        }),
        "application/json",
      ],
    });
    await expect(offlineHtml({ id: "incomplete" })).rejects.toThrow("完整论文资源");
    await expect(loadCompleteReport({ id: "incomplete" })).rejects.toThrow(
      "完整论文资源",
    );
  });

  it("embeds the report in the served page, safely", async () => {
    serve({
      "/": [TEMPLATE, "text/html"],
      "/a.json": [JSON.stringify(report), "application/json"],
    });
    const html = await offlineHtml({ id: "a", data_url: "/a.json" });
    const slot =
      /<script id="paperlens-data" type="application\/json">([\s\S]*?)<\/script>/;
    const data = slot.exec(html)?.[1] ?? "";
    expect(JSON.parse(data)).toEqual(report);
    expect(data).not.toContain("<");
    expect(html.replace(slot, "")).toBe(TEMPLATE.replace(slot, ""));
  });

  it("carries over the saved report's open-source notices", async () => {
    const saved = TEMPLATE.replace(
      "null",
      JSON.stringify(report).replace(/</g, "\\u003c"),
    ).replace("</body></html>", `${NOTICES}</body></html>`);
    serve({
      "/": [TEMPLATE, "text/html"],
      "/b.json": [JSON.stringify(report), "application/json"],
      "/r/b": [saved, "text/html"],
    });
    const html = await offlineHtml({ id: "b", data_url: "/b.json", report_url: "/r/b" });
    expect(html.endsWith(`${NOTICES}</body></html>`)).toBe(true);
  });

  it("refuses a page without an empty data slot", async () => {
    serve({
      "/": ["<html><body></body></html>", "text/html"],
      "/c.json": [JSON.stringify(report), "application/json"],
    });
    await expect(offlineHtml({ id: "c", data_url: "/c.json" })).rejects.toThrow(
      "离线文件",
    );
  });
});
