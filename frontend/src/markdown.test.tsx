import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { Markdown, parseInline, parseMarkdown } from "./markdown";

// MathML sanitisation is covered by reader/browser checks; SSR has no DOMParser.
vi.mock("./tex", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./tex")>()),
  texToMathML: vi.fn((tex: string) =>
    tex === "invalid" ? null : "<math><mi>x</mi></math>",
  ),
}));

describe("assistant Markdown mathematics", () => {
  it("protects TeX from emphasis and citation parsing while preserving literal code", () => {
    expect(parseInline("See $x*[1]$ [2] and `$x$`")).toEqual([
      { t: "text", v: "See " },
      { t: "math", v: "x*[1]", display: false },
      { t: "text", v: " " },
      { t: "cite", n: [2] },
      { t: "text", v: " and " },
      { t: "code", v: "$x$" },
    ]);
    expect(parseInline("\\(x_i\\) and $$y^2$$").filter((p) => p.t === "math")).toEqual([
      { t: "math", v: "x_i", display: false },
      { t: "math", v: "y^2", display: true },
    ]);
  });

  it("keeps multiline display equations whole and fenced code literal", () => {
    expect(parseMarkdown("Before\n$$\nx_i +\ny_i\n$$\n\n```tex\n$$x$$\n```")).toEqual([
      { t: "p", lines: ["Before"] },
      { t: "math", v: "x_i +\ny_i" },
      { t: "code", v: "$$x$$" },
    ]);
  });

  it("uses the existing safe MathML renderer and falls back to source", () => {
    const html = renderToStaticMarkup(
      <Markdown
        text={
          "Inline $x_i$; `$x_i$`\n\n$$\ny\n$$\n\n$invalid$\n\n<script>alert(1)</script>"
        }
      />,
    );
    expect(html.match(/<math>/g)).toHaveLength(2);
    expect(html).toContain('class="tex-inline"');
    expect(html).toContain('class="tex-display"');
    expect(html).toContain("<code>$x_i$</code>");
    expect(html).toContain('<code class="tex-source">invalid</code>');
    expect(html).not.toContain("<script>");
  });

  it("renders equations inside emphasis without losing surrounding formatting", () => {
    const html = renderToStaticMarkup(
      <Markdown text="**Energy $x_i$** and *value $y$*" />,
    );
    expect(html).toContain("<strong>Energy <span");
    expect(html).toContain("<em>value <span");
    expect(html.match(/<math>/g)).toHaveLength(2);
  });
});
