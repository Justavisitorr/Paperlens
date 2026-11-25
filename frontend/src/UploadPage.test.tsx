import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { MathReviewBudget } from "./UploadPage";

vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});

describe("upload math review budget", () => {
  it("shows the server's real batch count and shared request cap for each tier", () => {
    for (const batches of [0, 1, 2, 4]) {
      const html = renderToStaticMarkup(<MathReviewBudget batches={batches} />);
      expect(html).toContain(batches ? `公式审校 ${batches} 批` : "公式审校已关闭");
      expect(html).not.toContain("每批 6 图");
    }
  });
  it("does not invent a budget when older servers omit it", () => {
    expect(renderToStaticMarkup(<MathReviewBudget />)).toBe("");
  });
});
