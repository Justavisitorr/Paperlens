import { describe, expect, it } from "vitest";
import { tierPreview } from "./tiers";

describe("tierPreview", () => {
  it("halves rounding down and doubles", () => {
    expect(tierPreview(5, { min: 0, max: 40 })).toEqual({
      economy: 2,
      standard: 5,
      detailed: 10,
    });
  });
  it("stays inside the field range", () => {
    expect(tierPreview(30, { min: 0, max: 40 }).detailed).toBe(40);
    expect(tierPreview(600, { min: 512, max: 48000 }).economy).toBe(512);
    expect(tierPreview(5, { min: 5, max: 600 }).economy).toBe(5);
  });
});

it("keeps zero unlimited and positive small caps finite", () => {
  expect(tierPreview(0, { min: 0, max: 40 })).toEqual({
    economy: 0,
    standard: 0,
    detailed: 0,
  });
  expect(tierPreview(1, { min: 0, max: 40 }).economy).toBe(1);
});
