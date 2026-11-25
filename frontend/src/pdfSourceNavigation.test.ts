import { describe, expect, it } from "vitest";
import {
  pdfCenterScroll,
  pdfPageDraft,
  pdfViewCenter,
  sourceTargetBox,
} from "./pdfSourceNavigation";

describe("PDF source navigation", () => {
  it("uses only the physical page for model-estimated coordinates", () => {
    const box = { x0: 0.1, y0: 0.2, x1: 0.4, y1: 0.5 };
    expect(sourceTargetBox("openai-pdf", box)).toBeUndefined();
    expect(sourceTargetBox("model-page-images", box)).toBeUndefined();
    expect(sourceTargetBox("docling+pdfplumber", box)).toBe(box);
    expect(sourceTargetBox("pdfplumber", null)).toBeUndefined();
  });
  it("keeps the visible source point at the same center after zooming", () => {
    const viewport = { left: 10, top: 20, width: 400, height: 300 };
    const before = { left: -90, top: -680, width: 600, height: 1200 };
    const center = pdfViewCenter(before, viewport);
    expect(center.x).toBe(0.5);
    expect(center.y).toBeCloseTo(850 / 1200);
    const after = { left: -90, top: -680, width: 1200, height: 2400 };
    const scroll = pdfCenterScroll(after, viewport, { left: 120, top: 720 }, center);
    expect(scroll.left).toBe(420);
    expect(scroll.top).toBe(1570);
    const moved = {
      ...after,
      left: after.left - (scroll.left - 120),
      top: after.top - (scroll.top - 720),
    };
    expect(pdfViewCenter(moved, viewport)).toEqual(center);
  });
  it("locates right-column targets in both dimensions", () => {
    expect(
      pdfCenterScroll(
        { left: 20, top: 20, width: 1600, height: 2400 },
        { left: 0, top: 0, width: 400, height: 300 },
        { left: 0, top: 0 },
        { x: 0.75, y: 0.5 },
      ),
    ).toEqual({ left: 1020, top: 1070 });
  });
  it("validates only a submitted full integer draft, preserving empty edits", () => {
    expect(pdfPageDraft("", 100)).toBeNull();
    expect(pdfPageDraft("12", 100)).toBe(12);
    expect(pdfPageDraft(" 003 ", 100)).toBe(3);
    for (const invalid of ["0", "101", "-1", "2.5", "1e1", "3abc", " "])
      expect(pdfPageDraft(invalid, 100)).toBeNull();
  });
});
