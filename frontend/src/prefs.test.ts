import { describe, expect, it } from "vitest";
import { interfaceFontSize, normalizePrefs } from "./prefs";

describe("interface font size preferences", () => {
  it("migrates both old size choices and keeps an explicit px preference", () => {
    expect(normalizePrefs(null).noteAIReview).toBe(false);
    expect(normalizePrefs({ theme: "dark" }).noteAIReview).toBe(false);
    expect(normalizePrefs({ noteAIReview: true }).noteAIReview).toBe(true);
    expect(normalizePrefs(null).uiFontSize).toBe(13);
    expect(normalizePrefs({ textSize: "standard" }).uiFontSize).toBe(13);
    expect(normalizePrefs({ textSize: "large" }).uiFontSize).toBe(14);
    expect(normalizePrefs({ textSize: "large", uiFontSize: 19 }).uiFontSize).toBe(19);
  });
  it("sanitizes invalid stored sizes and constrains the numeric range", () => {
    expect(interfaceFontSize(NaN)).toBe(13);
    expect(normalizePrefs({ uiFontSize: Infinity, textSize: "large" }).uiFontSize).toBe(
      14,
    );
    expect(interfaceFontSize(900)).toBe(24);
    expect(interfaceFontSize(-1)).toBe(12);
    expect(interfaceFontSize(15.8)).toBe(16);
  });
});
