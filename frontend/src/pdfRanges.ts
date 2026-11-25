import { resourcePath } from "./reportAssets";

export const PDF_RANGE_BYTES = 65536;

/** Use fetch so the hosted workspace identity header also covers PDF byte ranges. */
export async function readPdfRange(
  source: string,
  begin: number,
  end: number,
  signal: AbortSignal,
  knownLength?: number,
): Promise<{ bytes: Uint8Array; length: number; complete: boolean }> {
  const path = resourcePath(source, "pdf");
  if (!path) throw new Error("原始 PDF 资源地址无效。");
  if (
    !Number.isSafeInteger(begin) ||
    !Number.isSafeInteger(end) ||
    begin < 0 ||
    end <= begin
  )
    throw new Error("原始 PDF 的读取范围无效。");
  const response = await fetch(path, {
    signal,
    credentials: "same-origin",
    headers: { Range: `bytes=${begin}-${end - 1}` },
  });
  if (!response.ok)
    throw new Error(`原始 PDF 暂时无法读取（${response.status}），请刷新页面后重试。`);
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("application/pdf"))
    throw new Error("服务器未返回原始 PDF。");
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (response.status === 200) {
    // Older servers may ignore Range. Keep their existing full-PDF behavior.
    if (!bytes.byteLength || (knownLength && bytes.byteLength !== knownLength))
      throw new Error("原始 PDF 已变化或数据不完整，请刷新后重试。");
    return { bytes, length: bytes.byteLength, complete: true };
  }
  const match = response.headers
    .get("content-range")
    ?.match(/^bytes (\d+)-(\d+)\/(\d+)$/);
  if (response.status !== 206 || !match)
    throw new Error("原始 PDF 分段响应缺少有效范围。");
  const [, first, last, total] = match.map(Number);
  if (
    !Number.isSafeInteger(total) ||
    total <= 0 ||
    first !== begin ||
    last !== Math.min(end, total) - 1 ||
    bytes.byteLength !== last - first + 1 ||
    (knownLength && total !== knownLength)
  )
    throw new Error("原始 PDF 分段不完整或文件已变化，请刷新后重试。");
  return { bytes, length: total, complete: first === 0 && bytes.byteLength === total };
}
