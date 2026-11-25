import { describe, expect, it, vi } from "vitest";
vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "file:" };
});
import { readChatStream } from "./chatStream";
import { droppedPassage } from "./chat";

const reply = {
  answer: "核对后的回答 [1]",
  grounded: true,
  quotes: [{ block_id: "b1" }],
};
const responseOf = (frames: unknown[], bytes = 1) => {
  const encoded = new TextEncoder().encode(
    frames.map((frame) => JSON.stringify(frame)).join("\n") + "\n",
  );
  let index = 0;
  return new Response(
    new ReadableStream({
      pull(controller) {
        if (index >= encoded.length) {
          controller.close();
          return;
        }
        controller.enqueue(encoded.slice(index, index + bytes));
        index += bytes;
      },
    }),
    { headers: { "content-type": "application/x-ndjson" } },
  );
};

describe("true streamed chat frames", () => {
  it("decodes split UTF-8 bytes and keeps final verified sources", async () => {
    const seen: string[] = [];
    const final = await readChatStream(
      responseOf([
        { type: "text", text: "中" },
        { type: "text", text: "中文😀 [99]" },
        { type: "done", reply },
      ]),
      (text) => seen.push(text),
    );
    expect(seen).toEqual(["中", "中文😀 [99]"]);
    expect(final).toEqual(reply);
    expect(final.answer).not.toContain("99");
  });
  it("rejects disconnected provisional content instead of treating it as an answer", async () => {
    await expect(
      readChatStream(responseOf([{ type: "text", text: "不完整" }]), () => {}),
    ).rejects.toThrow(/未收到核对/);
  });
  it("surfaces error frames after partial output", async () => {
    await expect(
      readChatStream(
        responseOf([
          { type: "text", text: "生成中" },
          { type: "error", message: "模型连接中断" },
        ]),
        () => {},
      ),
    ).rejects.toThrow("模型连接中断");
  });
  it("cancels the actual reader while waiting for more bytes", async () => {
    const cancel = vi.fn();
    const stream = new ReadableStream({ cancel });
    const controller = new AbortController();
    const pending = readChatStream(
      new Response(stream, { headers: { "content-type": "application/x-ndjson" } }),
      () => {},
      controller.signal,
    );
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(cancel).toHaveBeenCalledOnce();
  });
  it("rejects malformed final frames without retaining unvalidated quotes", async () => {
    await expect(
      readChatStream(responseOf([{ type: "done", reply: { answer: "bad" } }]), () => {}),
    ).rejects.toThrow(/不兼容/);
  });
});

describe("dragged source references", () => {
  const source = {
    kind: "passage",
    paper_id: "p1",
    paper_title: "论文",
    block_id: "b2",
    start: 3,
    end: 6,
    text: "中😀",
    page: 2,
  };
  it("retains UTF-16 offsets and ignores untrusted extra fields", () => {
    expect(droppedPassage(JSON.stringify({ ...source, isVerified: true }))).toEqual(
      source,
    );
  });
  it("rejects ordinary text and mismatched offsets", () => {
    expect(droppedPassage("中😀")).toBeNull();
    expect(droppedPassage(JSON.stringify({ ...source, end: 5 }))).toBeNull();
    expect(droppedPassage(JSON.stringify({ ...source, start: -1 }))).toBeNull();
  });
});
