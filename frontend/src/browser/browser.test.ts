import { afterEach, describe, expect, it, vi } from "vitest";
import { analyzeBatch, parseAnswer, providers } from "./provider";
import { locateQuote, makeReport } from "./report";
import type { Block } from "../types";

const block = (id: string, text: string): Block => ({
  id,
  text,
  raw_text: text,
  page: 1,
  bbox: { x0: 0.1, y0: 0.2, x1: 0.8, y1: 0.3 },
  section: "",
  order: 0,
  kind: "text",
});
const row = {
  page: 1,
  section: "Study",
  transcription: "",
  findings: [
    {
      title: "Measured yield",
      kind: "evidence" as const,
      detail: "The yield was 37 percent.",
      quote: "yield was 37 percent",
    },
  ],
};
const result = {
  title: "Field study",
  summary: "37 percent",
  question: "What was the yield?",
  pages: [row],
};
const signal = new AbortController().signal;
const input = [{ number: 1, image: "data:image/jpeg;base64,AA", hasText: true }];
afterEach(() => vi.unstubAllGlobals());

describe("browser PDF analysis", () => {
  it("requires exactly the submitted physical pages and restores their order", () => {
    expect(
      parseAnswer(
        JSON.stringify({ ...result, pages: [{ ...row, page: 2 }, row] }),
        [1, 2],
      ).pages.map((p) => p.page),
    ).toEqual([1, 2]);
    for (const pages of [[row, row], [], [{ ...row, page: 8 }], [{ page: 1 }]])
      expect(() => parseAnswer(JSON.stringify({ ...result, pages }), [1])).toThrow();
  });
  it("matches verbatim and normalized text without treating a different value as support", () => {
    const blocks = [block("a", "The yield was"), block("b", "37 percent.")];
    expect(locateQuote("yield was", blocks)[0].exact).toBe(true);
    expect(locateQuote("yield was 37 percent", blocks).map((m) => m.block.id)).toEqual([
      "a",
      "b",
    ]);
    expect(locateQuote("yield was 73 percent", blocks)).toEqual([]);
  });
  it("keeps unmatched scan claims out of verified evidence", () => {
    const report = makeReport(
      {
        source: { data_url: "data:application/pdf;base64,AA", sha256: "a".repeat(64) },
        filename: "scan.pdf",
      },
      [
        {
          answer: {
            ...result,
            input: 123,
            output: 57,
            pages: [{ ...row, transcription: "The yield was 37 percent." }],
          },
          pages: [
            {
              meta: {
                number: 1,
                label: "i",
                width: 100,
                height: 200,
                text_chars: 0,
                status: "image_only",
                warnings: [],
              },
              blocks: [],
            },
          ],
        },
      ],
      "test-model",
      5,
      1,
    );
    expect(report.document.blocks[0].text_source).toBe("ocr");
    expect(report.analysis.nodes[1].support).toBe("unverified");
    expect(report.evidence).toHaveLength(0);
    expect(report.run.usage.prompt_tokens).toBe(123);
    expect(report.graphs[0].node_ids).toHaveLength(2);
  });
  it.each(["deepseek", "openai"] as const)(
    "uses the official %s API and reads the returned usage",
    async (provider) => {
      const raw = JSON.stringify(result);
      const body =
        provider === "deepseek"
          ? {
              choices: [{ message: { content: raw }, finish_reason: "stop" }],
              usage: { prompt_tokens: 20, completion_tokens: 10 },
            }
          : {
              status: "completed",
              output: [{ content: [{ type: "output_text", text: raw }] }],
              usage: { input_tokens: 20, output_tokens: 10 },
            };
      const call = vi.fn().mockResolvedValue(Response.json(body));
      vi.stubGlobal("fetch", call);
      const answer = await analyzeBatch(provider, "test-key", input, signal);
      expect(call).toHaveBeenCalledOnce();
      const [url, init] = call.mock.calls[0];
      expect(url).toBe(providers[provider].url);
      expect(init).toMatchObject({
        credentials: "omit",
        redirect: "error",
        referrerPolicy: "no-referrer",
      });
      expect(init.body).not.toContain("test-key");
      expect(init.body).toContain(input[0].image);
      expect(answer).toMatchObject({ title: result.title, input: 20, output: 10 });
    },
  );
  it("leaves retries to the visitor when a provider denies or rate-limits a request", async () => {
    const call = vi.fn().mockResolvedValue(new Response(null, { status: 429 }));
    vi.stubGlobal("fetch", call);
    await expect(analyzeBatch("deepseek", "test", input, signal)).rejects.toThrow(/429/);
    expect(call).toHaveBeenCalledOnce();
  });
});
