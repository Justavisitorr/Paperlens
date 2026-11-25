import { afterEach, describe, expect, it, vi } from "vitest";
import { nextSpeedMode, normalizeSpeedMode } from "./processingSpeed";
vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
import { estimateProcessing, submitJob, startImport, jobAction } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("processing speed modes", () => {
  it("cycles once per native click or keyboard activation", () => {
    const first = nextSpeedMode("normal");
    const second = nextSpeedMode(first);
    expect(first).toBe("fast");
    expect(second).toBe("ultra");
    expect(nextSpeedMode(second)).toBe("normal");
    expect(normalizeSpeedMode(false)).toBe("normal");
    expect(normalizeSpeedMode(true)).toBe("fast");
    expect(normalizeSpeedMode("ultra")).toBe("ultra");
  });

  it.each(["normal", "fast", "ultra"] as const)(
    "submits %s to estimates, PDFs and imports",
    async (mode) => {
      const fetchMock = vi.fn(
        async (_url: string, _init: RequestInit) =>
          new Response(
            JSON.stringify({ id: "job", pages: 8, pages_assumed: false, profiles: [] }),
            { headers: { "content-type": "application/json" } },
          ),
      );
      vi.stubGlobal("fetch", fetchMock);
      await estimateProcessing(undefined, undefined, mode);
      await submitJob(new File(["pdf"], "paper.pdf"), "standard", false, mode);
      await startImport("candidate", "detailed", false, mode);
      for (const call of fetchMock.mock.calls.slice(0, 2)) {
        const body = call[1].body as FormData;
        expect(body.get("speed_mode")).toBe(mode);
        expect(body.get("accelerated")).toBe(String(mode !== "normal"));
      }
      expect(JSON.parse(fetchMock.mock.calls[2][1].body as string)).toMatchObject({
        speed_mode: mode,
        accelerated: mode !== "normal",
      });
    },
  );

  it("confirms the running-job ultra action without a fixed concurrency claim", async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init: RequestInit) =>
        new Response(
          JSON.stringify({
            id: "job",
            speed_mode: "ultra",
            acceleration: { enabled: true, concurrency: 5 },
          }),
          { headers: { "content-type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const job = await jobAction("job", "ultra");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs/job/ultra");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({
      confirmed: true,
    });
    expect(job.acceleration?.concurrency).toBe(5);
  });
});
