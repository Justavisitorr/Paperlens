import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { analysisPieces, AnalysisText } from "./AnalysisText";
import { READING_MODES } from "./readerNavigation";
import { href, parseRoute } from "./router";
import { normalizePrefs } from "./prefs";

describe("analysis math and navigation", () => {
  it("recognises report math with and without delimiters without consuming prose", () => {
    const pieces = analysisPieces(
      String.raw`问题 P_{g,n} 与 ker q_{g,n}，以及 ker q = P 和 $x_i^2$。`,
    );
    expect(pieces.filter((p) => "tex" in p).map((p) => "tex" in p && p.tex)).toEqual([
      "P_{g,n}",
      String.raw`\ker q_{g,n}`,
      String.raw`\ker q = P`,
      "x_i^2",
    ]);
    expect(analysisPieces("user_name，README.md 和 20% 的正文")).toEqual([
      { text: "user_name，README.md 和 20% 的正文" },
    ]);
    expect(
      renderToStaticMarkup(<AnalysisText text="<script>alert(1)</script>" />),
    ).not.toContain("<script>");
  });
  it("uses a single five-view shortcut order and keeps explicit links distinct", () => {
    expect(READING_MODES.map(([mode]) => mode)).toEqual([
      "overview",
      "map",
      "text",
      "glossary",
      "quality",
    ]);
    expect(parseRoute("#/read/paper")).toMatchObject({ mode: "overview" });
    for (const [mode] of READING_MODES) {
      const route = { page: "read" as const, paperId: "paper", mode };
      expect(parseRoute(href(route))).toEqual(route);
    }
    expect(href({ page: "read", paperId: "paper", mode: "map" })).toBe(
      "#/read/paper/map",
    );
  });
  it("normalises the full-read preference and preserves legacy settings", () => {
    expect(normalizePrefs({ theme: "dark" }).fullReadMode).toBe("text");
    expect(normalizePrefs({}).lightPalette).toBe("clay");
    // Older saves named the previous default explicitly; they move once, later choices stick.
    const moved = normalizePrefs({ lightPalette: "default" });
    expect(moved.lightPalette).toBe("clay");
    expect(normalizePrefs({ ...moved, lightPalette: "default" }).lightPalette).toBe(
      "default",
    );
    expect(normalizePrefs({ fullReadMode: "map", theme: "dark" })).toMatchObject({
      fullReadMode: "map",
      theme: "dark",
    });
  });
});
