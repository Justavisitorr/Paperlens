import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  citationNumbers,
  citationTargets,
  referenceBacklinks,
  referenceIndex,
  referenceDestination,
  itemXrefKey,
  linkify,
  mostlyChinese,
  splitCaption,
  xrefKey,
  xrefKeys,
} from "./readerLinks";
import { splitTex, texRanges } from "./tex";
import type { ReadingItem } from "./types";

const item = (kind: ReadingItem["kind"], label: string): ReadingItem => ({
  id: `reading-${kind}`,
  kind,
  parts: [],
  page: 1,
  bbox: { x0: 0, y0: 0, x1: 1, y1: 1 },
  label,
  level: 1,
  asset: "",
  result_ids: [],
  visual_ids: [],
});

describe("splitTex", () => {
  it("reads inline and display TeX", () => {
    expect(splitTex("let $x^2$ be")).toEqual([
      { text: "let " },
      { tex: "x^2", display: false },
      { text: " be" },
    ]);
    expect(splitTex("$$a+b$$")).toEqual([{ tex: "a+b", display: true }]);
    expect(splitTex("\\(y\\)")).toEqual([{ tex: "y", display: false }]);
  });
  it("records raw delimiters at actual UTF-16 positions", () => {
    expect(texRanges("😀前 $x_i$ 后")).toEqual([
      { start: 4, end: 9, tex: "x_i", display: false },
    ]);
    expect(texRanges("costs $5 and $10 now")).toEqual([]);
  });
  it("leaves money and escaped dollars alone", () => {
    expect(splitTex("costs $5 and $10 now")).toEqual([{ text: "costs $5 and $10 now" }]);
    expect(splitTex("a \\$ sign")).toEqual([{ text: "a $ sign" }]);
  });
});

describe("cross-references", () => {
  it("indexes prose mentions by reference, while excluding code and mathematical indices", () => {
    const refs = new Map([
      ["2", { ...item("reference", "[2]"), id: "r2" }],
      ["3", { ...item("reference", "[3]"), id: "r3" }],
      ["4", { ...item("reference", "[4]"), id: "r4" }],
      ["Smith2020", { ...item("reference", "Smith"), id: "r5" }],
    ]);
    const body = [
      {
        ...item("paragraph", ""),
        id: "body1",
        page: 3,
        content: "Prior work [2–4]; repeated [3], and again [3]. Smith (2020).",
      },
      {
        ...item("paragraph", ""),
        id: "body2",
        page: 4,
        content: "Inline `a[2]`, math $[4]$, and x = [2, 3]; array[3].",
      },
      { ...item("code", ""), id: "code", content: "values = [2, 3, 4]" },
      { ...item("formula", ""), id: "math", content: "[2–4]" },
    ];
    const found = referenceBacklinks(body, new Map(), refs);
    expect(found.get("r2")).toMatchObject([{ itemId: "body1", page: 3 }]);
    expect(found.get("r3")).toHaveLength(3);
    expect(found.get("r3")?.map((hit) => hit.start)).toEqual([11, 27, 42]);
    expect(found.get("r4")).toHaveLength(1);
    expect(found.get("r5")).toMatchObject([{ itemId: "body1", page: 3 }]);
    expect(found.get("r2")?.[0].excerpt).toContain("Prior work [2–4]");
  });
  it("keys mentions in both languages", () => {
    expect(xrefKey("Fig. 3")).toBe("fig:3");
    expect(xrefKey("Table 2")).toBe("tab:2");
    expect(xrefKey("式 (2)")).toBe("eq:2");
    expect(xrefKey("Eq. (4)")).toBe("eq:4");
    expect(xrefKey("第 3.2 节")).toBe("sec:3.2");
    expect(xrefKey("第 3")).toBeNull();
    expect(xrefKey("附录 A")).toBe("app:A");
    expect(xrefKey("Algorithm 1")).toBe("alg:1");
  });
  it("expands citations and filters large ranges against existing references", () => {
    expect(citationNumbers("[3, 5–7]")).toEqual(["3", "5", "6", "7"]);
    expect(citationNumbers("[1-200]")).toHaveLength(200);
    expect(citationNumbers("[1–1000000000]", ["2", "4", "23"])).toEqual(["2", "4", "23"]);
    expect(citationNumbers("[2～4]", ["2", "4", "8"])).toEqual(["2", "4"]);
  });
  it("links complete object and citation ranges without changing UTF-16 text", () => {
    const targets = new Map([
      ["eq:5", item("formula", "(5)")],
      ["eq:6", item("formula", "(6)")],
      ["eq:7", item("formula", "(7)")],
      ["fig:2", item("figure", "图2")],
      ["fig:3", item("figure", "图3")],
      ["fig:4", item("figure", "图4")],
    ]);
    const ctx = {
      refs: new Map([
        ["2", {}],
        ["3", {}],
        ["4", {}],
      ]),
      targets,
    };
    const text = "😀式(5)~(7)、图2～4、[2–4]，普通 a~b、2~4、算法\n1) 保持原文。";
    const html = renderToStaticMarkup(<>{linkify(text, ctx)}</>);
    expect(html).toContain('data-xrefs="eq:5 eq:6 eq:7"');
    expect(html).toContain('data-xrefs="fig:2 fig:3 fig:4"');
    expect(html).toContain('data-cites="2 3 4"');
    expect(html.match(/<button/g)).toHaveLength(3);
    expect(html.replace(/<[^>]+>/g, "")).toBe(text);
    expect(xrefKeys("式(5)~(7)", ["eq:6", "eq:7", "fig:6"])).toEqual(["eq:6", "eq:7"]);
    expect(xrefKeys("a~b", targets.keys())).toEqual([]);
  });
  it("keys the items a mention points at", () => {
    const blocks = new Map();
    expect(itemXrefKey(item("figure", "Figure 2: Overview"), blocks)).toBe("fig:2");
    expect(itemXrefKey(item("table", "表 1 实验数据"), blocks)).toBe("tab:1");
    expect(itemXrefKey(item("heading", "3.2 Training"), blocks)).toBe("sec:3.2");
    expect(itemXrefKey(item("heading", "Appendix B Proofs"), blocks)).toBe("app:B");
    expect(itemXrefKey(item("formula", "(5)"), blocks)).toBe("eq:5");
    expect(itemXrefKey({ ...item("formula", "(3)"), content: "x_i^2" }, blocks)).toBe(
      "eq:3",
    );
    expect(itemXrefKey(item("paragraph", "Figure 2"), blocks)).toBeNull();
  });
  it("links without changing the words", () => {
    const ctx = {
      refs: new Map([["12", {}]]),
      targets: new Map([["fig:2", item("figure", "Figure 2")]]),
    };
    const text = "See Figure 2 and Table 9 [12, 13].";
    const html = renderToStaticMarkup(<>{linkify(text, ctx)}</>);
    expect(html).toContain('data-xref="fig:2"');
    expect(html).not.toContain("tab:9");
    expect(html).toContain('data-cite="12"');
    expect(html).not.toContain('data-cite="13"');
    expect(html.replace(/<[^>]+>/g, "")).toBe(text);
  });
});

describe("captions", () => {
  it("splits the number and an English twin", () => {
    expect(splitCaption("Table 2: Results on GLUE")).toEqual({
      tag: "Table 2",
      rest: "Results on GLUE",
      other: "",
    });
    expect(splitCaption("表 1 实验数据集 Table 1 Experimental datasets")).toEqual({
      tag: "表 1",
      rest: "实验数据集",
      other: "Table 1 Experimental datasets",
    });
    expect(splitCaption("A plain caption").tag).toBe("");
  });
  it("tells Chinese papers apart", () => {
    expect(mostlyChinese("我们".repeat(30))).toBe(true);
    expect(mostlyChinese("We propose a model ".repeat(10))).toBe(false);
  });
});

describe("external bibliography scope", () => {
  const refs = new Map(["Pix26", "CLS", "DM", "DR", "EGH+"].map((key) => [key, {}]));
  const targets = new Map([["sec:2.2", item("heading", "2.2 Local section")]]);
  it("links the whole external locator before any local Section mention", () => {
    const citation = "[Pix26, Theorem 1.1 and Section 2.2]";
    const html = renderToStaticMarkup(
      <>{linkify(`结论${citation}；本文 Section 2.2`, { refs, targets })}</>,
    );
    expect(html).toContain('data-cite="Pix26"');
    expect(html.match(/data-xref=/g)).toHaveLength(1);
    expect(html).toContain(`${citation}</button>`);
    expect(html).toContain("未定位外文原页");
  });
  it("accepts exact known labels across spaces, lines and combinations", () => {
    expect(
      citationTargets("[P i x 26, Theorem 1.1 and\nSection 2.2]", refs.keys()),
    ).toEqual(["Pix26"]);
    expect(citationTargets("[DM, DR; EGH+]", refs.keys())).toEqual(["DM", "DR", "EGH+"]);
    expect(citationTargets("[CLS, Theorems 1 and 7]", refs.keys())).toEqual(["CLS"]);
  });
  it("keeps formulas, unknown citations and ordinary parentheses as text", () => {
    const text = "[x,y] f(x,y) (ordinary text) [Unknown, Section 2.2]";
    const html = renderToStaticMarkup(<>{linkify(text, { refs, targets })}</>);
    expect(html).not.toContain("<button");
    expect(html).toBe(text);
  });
  it("requires an unambiguous existing author-year entry", () => {
    const ref = {
      ...item("reference", ""),
      id: "smith",
      content: "Smith, J. A title. 2020.",
    };
    const index = referenceIndex([ref], new Map());
    expect(citationTargets("(Smith et al., 2020)", index.keys())).toEqual(["Smith2020"]);
    expect(
      referenceIndex([ref, { ...ref, id: "other" }], new Map()).has("Smith2020"),
    ).toBe(false);
  });
  it("indexes real author-year formats, organizations, accents and year suffixes", () => {
    const reference = (id: string, content: string): ReadingItem => ({
      ...item("reference", ""),
      id,
      content,
    });
    const entries = [
      reference("ha", "D. Ha and J. Schmidhuber. World models. arXiv:1803.10122, 2018."),
      reference("h19", "D. Hafner, T. Lillicrap, and J. Ba. Dream to control. 2019."),
      reference("h20", "D. Hafner, T. Lillicrap, and J. Ba. Atari. 2020."),
      reference("h23", "D. Hafner, J. Ba, and T. Lillicrap. Models. 2023."),
      reference("z26b", "J. Zhang, S. Hu, and C. Lu. A paper. 2026b."),
      reference("z26c", "J. Zhang, B. Zhao, and W. Yang. Another paper. 2026c."),
      reference("z26d", "J. Zhang, B. Zhao, and W. Yang. A third paper. 2026d."),
      reference(
        "assumpcao",
        "H. Assumpção, D. Ferreira, and L. Campos. Codeevolve. 2025.",
      ),
      reference(
        "anthropic",
        "Anthropic. Mathematical capabilities. Aug. 2026. Accessed: 2026-08-13.",
      ),
    ];
    const index = referenceIndex(entries, new Map());
    expect(
      citationTargets(
        "(Ha and Schmidhuber, 2018; Hafner et al., 2019, 2020, 2023)",
        index.keys(),
      ),
    ).toEqual(["Ha&Schmidhuber2018", "Hafner2019", "Hafner2020", "Hafner2023"]);
    expect(citationTargets("(Ha and Other, 2018)", index.keys())).toEqual([]);
    expect(
      citationTargets(
        "(Zhang et al., 2026b,c; Assumpção et al., 2025; Anthropic, 2026)",
        index.keys(),
      ),
    ).toEqual(["Zhang2026b", "Zhang2026c", "Assumpção2025", "Anthropic2026"]);
    expect(citationTargets("(Zhang et al., 2026b,c,d)", index.keys())).toEqual([
      "Zhang2026b",
      "Zhang2026c",
      "Zhang2026d",
    ]);
    const text = "Ha and Schmidhuber (2018); (Zhang et al., 2026b,c).";
    const html = renderToStaticMarkup(
      <>{linkify(text, { refs: index, targets: new Map() })}</>,
    );
    expect(html).toContain('data-cite="Ha&amp;Schmidhuber2018"');
    expect(html).toContain('data-cite="Zhang2026b"');
    expect(html).toContain('data-cite="Zhang2026c"');
    expect(html.replace(/<[^>]+>/g, "")).toBe(text);
  });
  it("does not turn explicitly mathematical numeric intervals into citations", () => {
    const refs = new Map([
      ["1", {}],
      ["2", {}],
      ["3", {}],
    ]);
    const html = renderToStaticMarkup(
      <>
        {linkify("x ∈ [1,2], domain [-1,1], range [0,1], see [3]", {
          refs,
          targets: new Map(),
        })}
      </>,
    );
    expect(html).toContain("x ∈ [1,2]");
    expect(html).not.toContain('data-cite="1"');
    expect(html).toContain('data-cite="3"');
  });
});

describe("reference destinations", () => {
  it("opens explicitly cited arXiv IDs even in old reports without saved hrefs", () => {
    expect(
      referenceDestination("H. Assumpção. Codeevolve. arXiv:2510.14150, 2025."),
    ).toEqual({
      href: "https://arxiv.org/abs/2510.14150",
      direct: true,
    });
    expect(referenceDestination("Preprint arXiv:hep-th/9901001v2.")).toEqual({
      href: "https://arxiv.org/abs/hep-th/9901001v2",
      direct: true,
    });
  });
  it("preserves explicit links and labels unidentified work as a search", () => {
    expect(referenceDestination("Author. Article (doi:10.1234/a(b)).").href).toBe(
      "https://doi.org/10.1234/a(b)",
    );
    expect(referenceDestination("Visit https://example.org/paper).", "").href).toBe(
      "https://example.org/paper",
    );
    const unknown = referenceDestination("Author. Unidentified title. 2025.");
    expect(unknown.direct).toBe(false);
    expect(unknown.href).toContain("https://search.crossref.org/?q=");
    expect(referenceDestination("Bad arXiv:2026.11111.").direct).toBe(false);
  });
  it("keeps existing search pages labelled as search and does not guess ambiguous IDs", () => {
    const crossref = "https://search.crossref.org/?q=title";
    const scholar = "https://scholar.google.com/scholar?q=title";
    expect(referenceDestination("Title without identifier", crossref)).toEqual({
      href: crossref,
      direct: false,
    });
    expect(referenceDestination("Title without identifier", scholar)).toEqual({
      href: scholar,
      direct: false,
    });
    expect(referenceDestination("Preprint arXiv:2510.14150, 2025.", crossref)).toEqual({
      href: "https://arxiv.org/abs/2510.14150",
      direct: true,
    });
    expect(referenceDestination("Two works: 10.1234/a and 10.1234/b.").direct).toBe(
      false,
    );
    expect(
      referenceDestination("Two works: arXiv:2510.14150 and arXiv:2511.02864.").direct,
    ).toBe(false);
  });
});
