import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CodeBlock } from "./CodeBlock";

describe("paper code listings", () => {
  it("preserves whitespace and treats TeX and HTML as source text", () => {
    const text = "    int n = 2;\n\n    // $$x_i$$ <script>alert(1)</script>";
    const html = renderToStaticMarkup(<CodeBlock text={text} language="cpp" />);
    expect(html).toContain('aria-label="复制代码"');
    expect(html).toContain('class="hljs-type"');
    expect(html).toContain("\n\n    ");
    expect(html).toContain("$$x_i$$");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("katex");
  });
  it("keeps an unknown-language prompt listing as plain text", () => {
    const text = "  Read all results.\n  <instructions> are paper content.";
    const html = renderToStaticMarkup(
      <CodeBlock text={text} language="unknown" label="Listing 2" />,
    );
    expect(html).toContain("Listing 2");
    expect(html).toContain("<code>  Read all results.\n  &lt;instructions&gt;");
    expect(html).not.toContain("hljs-");
  });
  it("renders one panel while retaining every page's original selection surface", () => {
    const fragments = [
      { id: "code-a", text: "/* start", blockIds: ["a"] },
      { id: "code-b", text: "end */ int n = 3;", blockIds: ["b"] },
    ];
    const html = renderToStaticMarkup(
      <CodeBlock
        text={fragments.map((p) => p.text).join("\n")}
        fragments={fragments}
        language="cpp"
      />,
    );
    expect(html.match(/<pre /g)).toHaveLength(1);
    expect(html.match(/aria-label="复制代码"/g)).toHaveLength(1);
    expect(html).toContain('id="code-a"');
    expect(html).toContain('id="code-b"');
    expect(html).toContain('id="block-b"');
    expect(html.match(/data-reading-surface="original"/g)).toHaveLength(2);
    expect(html).toContain('class="hljs-comment">end */</span>');
  });
});
