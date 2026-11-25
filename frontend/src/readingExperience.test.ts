import { describe, expect, it } from "vitest";
import {
  itemSource,
  positionKey,
  readingPosition,
  readingPreferences,
  readingMeasure,
} from "./readingExperience";
import type { Block, ReadingItem } from "./types";

describe("continuous reader preferences and source anchors", () => {
  it("exposes only line widths that fit, so every enabled slider step changes the width", () => {
    const preference = { size: 24, measure: 46, leading: 1.9, font: "serif" } as const;
    const result = readingMeasure(preference, 790);
    expect(result).toEqual({
      min: 26,
      max: 32,
      value: 32,
      fitted: true,
      disabled: false,
    });
    for (let value = result.min; value <= result.max; value++) {
      expect(readingMeasure({ ...preference, measure: value }, 790).value).toBe(value);
      expect(value * preference.size).toBeLessThanOrEqual(790);
    }
    // A smaller window or a larger font must not overwrite the saved 46-character preference.
    expect(preference.measure).toBe(46);
    expect(readingMeasure(preference, 1200).value).toBe(46);
    expect(readingMeasure({ ...preference, size: 18 }, 790).max).toBe(43);
  });
  it("fits narrow screens without an invalid min/max range or an inert enabled slider", () => {
    const preference = readingPreferences(null);
    expect(readingMeasure(preference, 300)).toEqual({
      min: 16,
      max: 16,
      value: 16,
      fitted: true,
      disabled: true,
    });
    expect(readingMeasure(preference, 0).max).toBe(1);
    expect(readingMeasure(preference, null).max).toBe(46);
    expect(readingMeasure(preference, 1000).value).toBe(35);
  });
  it("rejects corrupt stored values and constrains controls to readable ranges", () => {
    expect(
      readingPreferences({ size: NaN, measure: 900, leading: -2, font: "other" }),
    ).toEqual({ size: 18, measure: 46, leading: 1.5, font: "serif" });
    expect(readingPreferences(null).font).toBe("serif");
    expect(
      readingPreferences({ size: 21, measure: 32, leading: 2.1, font: "sans" }),
    ).toEqual({ size: 21, measure: 32, leading: 2.1, font: "sans" });
  });
  it("resumes only existing positions in the same source version", () => {
    const ids = new Set(["paragraph-1"]);
    expect(readingPosition({ id: "paragraph-1", fraction: 2 }, ids)).toEqual({
      id: "paragraph-1",
      fraction: 1,
    });
    expect(readingPosition({ id: "deleted", fraction: 0.2 }, ids)).toBeNull();
    expect(readingPosition({ id: "paragraph-1", fraction: Infinity }, ids)).toBeNull();
    expect(readingPosition("paragraph-1", ids)).toBeNull();
    expect(positionKey("source-v1")).not.toBe(positionKey("source-v2"));
  });
  it("follows the actual visible source slice in a cross-page paragraph", () => {
    const first = { x0: 0.1, y0: 0.7, x1: 0.9, y1: 0.8 };
    const second = { x0: 0.1, y0: 0.2, x1: 0.9, y1: 0.3 };
    const sliced = { ...second, y1: 0.24 };
    const blocks = new Map<string, Block>([
      ["first", { id: "first", page: 2, bbox: first } as Block],
      ["second", { id: "second", page: 3, bbox: second } as Block],
    ]);
    const item = {
      page: 2,
      bbox: first,
      parts: [{ block_id: "first" }, { block_id: "second", bbox: sliced }],
    } as ReadingItem;
    expect(itemSource(item, blocks)).toEqual({ page: 2, box: first });
    expect(itemSource(item, blocks, 1)).toEqual({ page: 3, box: sliced });
    expect(itemSource(item, blocks, 20)).toEqual({ page: 2, box: first });
  });
});
