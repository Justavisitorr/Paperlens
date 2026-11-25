import { ApiError, type ChatReply } from "./api";

/** Server frames contain cumulative provisional text, then one validated reply. */
export async function readChatStream(
  response: Response,
  onText: (text: string) => void,
  signal?: AbortSignal,
): Promise<ChatReply> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      typeof body.detail === "string" ? body.detail : `请求失败（${response.status}）`,
      response.status,
      body.input_budget,
    );
  }
  if (
    !response.body ||
    !response.headers.get("content-type")?.includes("application/x-ndjson")
  )
    throw new Error("当前服务未返回可读取的回答流。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  const abort = () => {
    void reader.cancel();
  };
  signal?.addEventListener("abort", abort, { once: true });
  const checkAbort = () => {
    if (signal?.aborted) throw new DOMException("已停止生成。", "AbortError");
  };
  const frame = (line: string): ChatReply | undefined => {
    if (!line.trim()) return;
    let item;
    try {
      item = JSON.parse(line);
    } catch {
      throw new Error("回答流格式不完整，请重试。");
    }
    if (item.type === "error") throw new Error(item.message || "回答生成中断，请重试。");
    if (
      item.type === "text" &&
      typeof item.text === "string" &&
      item.text.length <= 12000
    ) {
      onText(item.text);
      return;
    }
    if (
      item.type === "done" &&
      item.reply &&
      typeof item.reply.answer === "string" &&
      Array.isArray(item.reply.quotes) &&
      typeof item.reply.grounded === "boolean"
    )
      return item.reply;
    throw new Error("回答流格式不兼容，请重试。");
  };
  try {
    while (true) {
      checkAbort();
      const { value, done } = await reader.read();
      checkAbort();
      buffer += decoder.decode(value, { stream: !done });
      if (buffer.length > 200_000) throw new Error("回答超过大小上限。");
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const reply = frame(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
        if (reply) return reply;
      }
      if (done) {
        const reply = frame(buffer);
        if (reply) return reply;
        throw new Error("回答连接已中断，未收到核对后的结果。请重试。");
      }
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export async function askPapersStream(
  body: unknown,
  signal: AbortSignal,
  onText: (text: string) => void,
): Promise<ChatReply> {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return readChatStream(response, onText, signal);
}
