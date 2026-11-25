import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
import { MathReviewBudget, ProcessingTierPicker } from "./ProcessingTierPicker";
import { UploadPage } from "./UploadPage";
import type { JobState, ProcessingProfiles } from "./api";

describe("three-state speed controls", () => {
  it.each(["normal", "fast", "ultra"] as const)(
    "renders %s with accurate icons and accessible state",
    (mode) => {
      const html = renderToStaticMarkup(
        <ProcessingTierPicker
          profile="standard"
          catalog={null}
          error=""
          disabled={false}
          speedMode={mode}
          onSpeedModeChange={() => {}}
          onChange={() => {}}
        />,
      );
      expect(html.match(new RegExp(`data-speed-mode="${mode}"`, "g"))).toHaveLength(3);
      expect(
        html.match(new RegExp(`aria-pressed="${mode !== "normal"}"`, "g")),
      ).toHaveLength(3);
      const buttons = [
        ...html.matchAll(
          /<button[^>]*class="tier-accelerate"[^>]*>([\s\S]*?)<\/button>/g,
        ),
      ];
      expect(buttons).toHaveLength(3);
      for (const [, body] of buttons) {
        expect(body.match(/<svg/g)).toHaveLength(1);
        expect(body).toContain('width="16" height="16"');
        const visible = body.replace(/<defs[\s\S]*?<\/defs>/g, "");
        expect(visible.match(/<(?:path|polygon|polyline)\b/g)).toHaveLength(
          mode === "ultra" ? 2 : 1,
        );
        if (mode === "ultra") {
          const maskId = body.match(/<mask[^>]*id="([^"]+)"/)?.[1];
          expect(maskId).toBeTruthy();
          expect(visible).toContain(`mask="url(#${maskId})"`);
          expect(body).not.toContain("--icon-cutout");
        }
      }
      if (mode === "ultra") {
        const maskIds = [...html.matchAll(/<mask[^>]*id="([^"]+)"/g)].map(
          (match) => match[1],
        );
        expect(new Set(maskIds).size).toBe(3);
      }
      expect(html).toContain("按回车或空格也可切换");
      expect(html).not.toMatch(
        /<button[^>]*role="radio"[^>]*>(?:(?!<\/button>)[\s\S])*<button/,
      );
    },
  );

  it("shows the server's actual ultra concurrency and preserves legacy fast jobs", () => {
    const jobs = [
      {
        id: "a",
        source_name: "ultra.pdf",
        status: "running",
        stage: "reading",
        message: "Working",
        speed_mode: "ultra",
        acceleration: { enabled: true, concurrency: 5 },
      },
      {
        id: "b",
        source_name: "old.pdf",
        status: "running",
        stage: "reading",
        message: "Working",
        acceleration: { enabled: true, concurrency: 3 },
        actions: ["ultra"],
      },
    ] as JobState[];
    const html = renderToStaticMarkup(
      <UploadPage
        staged={[]}
        jobs={jobs}
        jobsSource="native"
        sessionJobs={new Set()}
        busy={false}
        error=""
        canUpload={false}
        onPick={() => {}}
        onDropFiles={() => {}}
        onUnstage={() => {}}
        onStart={() => {}}
        onAction={() => {}}
      />,
    );
    expect(html).toContain("极速 · 5 路");
    expect(html).toContain("加速 · 3 路");
    expect(html).toContain('aria-label="极速 old.pdf"');
    expect(html).not.toContain("最多 3 路并行");
  });
});

describe("AI direct reading budgets", () => {
  const catalog: ProcessingProfiles = {
    default: "standard",
    cost_note: "",
    profiles: [
      {
        id: "standard",
        label: "标准",
        description: "按当前设置处理。",
        unlimited_limits: ["看图页数", "公式数量", "重排批次"],
        limits: {
          ai_direct: true,
          max_requests: 100,
          input_chars: 10000,
          output_tokens: 8000,
          reading_output_tokens: 8000,
          vision_pages: 0,
          vision_enabled: true,
          code_blocks: true,
          math_regions: 0,
          math_seconds: 0,
          inline_math_regions: 0,
          transcription_regions: 0,
          math_enabled: true,
          math_review_batches: 2,
          chart_crops_per_page: 2,
        },
      },
    ],
  };

  it("shows disabled page review without inventing a six-image allowance", () => {
    expect(renderToStaticMarkup(<MathReviewBudget batches={0} />)).toContain(
      "公式审校已关闭。",
    );
    expect(renderToStaticMarkup(<MathReviewBudget />)).toBe("");
  });

  it.each([false, true])(
    "shows whole-paper reading and page review in compact=%s",
    (compact) => {
      const html = renderToStaticMarkup(
        <ProcessingTierPicker
          profile="standard"
          catalog={catalog}
          error=""
          disabled={false}
          compact={compact}
          onChange={() => {}}
        />,
      );
      expect(html).toContain("AI 直读 · 全文");
      expect(html).toContain("随原文保留");
      expect(html).toContain("公式审校 2 批，按整页核对。");
      for (const obsolete of [
        "公式补救",
        "正文重排",
        "行内公式",
        "每批 6 图",
        "图表局部放大",
        "不限",
      ]) {
        expect(html).not.toContain(obsolete);
      }
    },
  );
});
