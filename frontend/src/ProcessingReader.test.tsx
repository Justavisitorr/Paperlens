import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";
import { ProcessingStatus } from "./ProcessingReader";
import { jobSourcePdf, type JobState } from "./api";
import { href, parseRoute } from "./router";
import { UploadPage } from "./UploadPage";

vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
vi.mock("./PdfSource", () => ({ PdfSource: () => null }));
vi.mock("./pdfRuntime", () => ({ sourceDocument: vi.fn() }));
afterEach(() => vi.unstubAllGlobals());

const job: JobState = {
  id: "a".repeat(32),
  source_name: "论文.pdf",
  status: "running",
  stage: "reading",
  message: "正在整理第 3 页",
  progress: 0.2,
  source_url: `/api/jobs/${"a".repeat(32)}/source.pdf`,
  actions: ["pause", "cancel"],
};

it("preserves a refreshable original-reader route and rejects invalid task ids", () => {
  const route = { page: "processing", jobId: job.id } as const;
  expect(parseRoute(href(route))).toEqual(route);
  expect(parseRoute("#/processing/../../private")).toBeNull();
});

it("shows processing progress, then offers results without replacing the original reader", () => {
  const render = (current: JobState) =>
    renderToStaticMarkup(<ProcessingStatus job={current} onAction={vi.fn()} />);
  expect(render(job)).toContain("正在整理第 3 页");
  expect(render(job)).toContain("暂停");
  expect(render(job)).not.toContain("打开整理结果");
  for (const status of ["complete", "partial"] as const) {
    const html = render({ ...job, status, paper_id: "result-id", actions: [] });
    expect(html).toContain("#/read/result-id/text");
    expect(html).toContain("可以继续读原文");
  }
  for (const status of ["failed", "cancelled"] as const) {
    const html = render({ ...job, status, actions: ["retry"] });
    expect(html).toContain("原文仍可阅读");
    expect(html).toContain("重试未完成步骤");
  }
});

it("makes original reading available on active and failed upload cards", () => {
  for (const status of ["running", "failed"] as const) {
    const html = renderToStaticMarkup(
      <UploadPage
        staged={[]}
        jobs={[{ ...job, status }]}
        jobsSource="native"
        sessionJobs={new Set()}
        busy={false}
        error=""
        canUpload={false}
        onPick={vi.fn()}
        onDropFiles={vi.fn()}
        onUnstage={vi.fn()}
        onStart={vi.fn()}
        onAction={vi.fn()}
      />,
    );
    expect(html).toContain(`href="#/processing/${job.id}"`);
    expect(html).toContain("阅读原文");
  }
});

it("fetches the fixed scoped PDF endpoint, not a returned arbitrary URL", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(
      new Response("%PDF-1.7", { headers: { "content-type": "application/pdf" } }),
    );
  vi.stubGlobal("fetch", fetch);
  expect(await (await jobSourcePdf(job.id)).text()).toBe("%PDF-1.7");
  expect(fetch).toHaveBeenCalledWith(`/api/jobs/${job.id}/source.pdf`, {
    signal: undefined,
  });
  await expect(jobSourcePdf("../private")).rejects.toThrow("任务编号无效");
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("reports missing files and login pages as errors before sending them to PDF.js", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: "原始 PDF 已移走" }), { status: 404 }),
      ),
  );
  await expect(jobSourcePdf(job.id)).rejects.toThrow("原始 PDF 已移走");
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response("login", { headers: { "content-type": "text/html" } }),
      ),
  );
  await expect(jobSourcePdf(job.id)).rejects.toThrow("确认登录状态");
});
