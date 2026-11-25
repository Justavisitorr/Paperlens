import katex from "katex";
import { describe, expect, it } from "vitest";
import { splitTex, stripMathDelimiters } from "./tex";

const expression = String.raw`\theta^{t+1}=\theta^t-\eta\left(\frac{\partial}{\partial\theta^t}\ell(\hat{y}(x_i|\Theta^t),y_i)+2\lambda\theta^t\right)`;

describe("formula field delimiters", () => {
  it.each([
    expression,
    `$${expression}$`,
    `$ \n${expression}\n $`,
    `$$\n${expression}\n$$`,
    `\\[${expression}\\]`,
    `\\(${expression}\\)`,
    `\x60\x60\x60latex\n$$${expression}$$\n\x60\x60\x60`,
  ])("renders wrapped model output without changing the expression", (input) => {
    const tex = stripMathDelimiters(input);
    expect(tex).toBe(expression);
    expect(katex.renderToString(tex, { output: "mathml", throwOnError: true })).toContain(
      "<math",
    );
  });

  it.each([
    String.raw`\$100`,
    "$a$ + $b$",
    "text $a$",
    "$a",
    String.raw`\begin{aligned}a&=b\\c&=d\end{aligned}`,
  ])(
    "preserves literal dollars, separate expressions and matrix row separators: %s",
    (input) => {
      expect(stripMathDelimiters(input)).toBe(input);
    },
  );

  it("preserves body delimiters and UTF-16 positions outside formula fields", () => {
    expect(splitTex("正文 $x_i$；$$y^2$$；\\(z\\)；\\[w\\]")).toEqual([
      { text: "正文 " },
      { tex: "x_i", display: false },
      { text: "；" },
      { tex: "y^2", display: true },
      { text: "；" },
      { tex: "z", display: false },
      { text: "；" },
      { tex: "w", display: true },
    ]);
  });
});
