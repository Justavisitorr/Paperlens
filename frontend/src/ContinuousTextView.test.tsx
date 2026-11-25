import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  captureCitationPosition,
  ContinuousTextView,
  ReadingLanguageSwitch,
  restoreReadingPosition,
} from "./ContinuousTextView";
import type { Annotation } from "./api";
import type { Block, DashboardReport, ReadingItem, ReadingTranscription } from "./types";

vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});
vi.mock("./PdfSource", () => ({ PdfSource: () => null }));

const bbox = { x0: 0.1, y0: 0.1, x1: 0.9, y1: 0.2 };
const block = (id: string, text: string): Block => ({
  id,
  text,
  raw_text: text,
  bbox,
  page: 1,
  kind: "text",
  order: 0,
  section: "",
});
const item = (b: Block, kind: ReadingItem["kind"] = "paragraph"): ReadingItem => ({
  id: `reading-${b.id}`,
  kind,
  parts: [{ block_id: b.id, start: 0, end: b.text.length, join: "" }],
  page: 1,
  bbox,
  label: "",
  level: 1,
  asset: "",
  result_ids: [],
  visual_ids: [],
});
const report = (blocks: Block[], items: ReadingItem[]): DashboardReport =>
  ({
    document: { title: "Paper", blocks, pages: [{ number: 1, label: "" }] },
    analysis: { title: "" },
    reading: {
      version: "2",
      source_version: "source",
      items,
      references: [],
      information: {},
      hidden_ids: [],
      warnings: [],
    },
    quantitative_results: [],
    visuals: [],
  }) as unknown as DashboardReport;
const render = (r: DashboardReport, query = "", marks: Annotation[] = []) =>
  renderToStaticMarkup(
    <ContinuousTextView
      report={r}
      query={query}
      onClearQuery={() => {}}
      onExport={() => {}}
      outlineSlot={null}
      marks={marks}
    />,
  );

describe("returning from a citation", () => {
  it("restores the exact pre-chooser scroll position and falls back to the anchor after reflow", () => {
    const pane = { scrollTop: 0, clientWidth: 900 };
    let anchorOffset = 60;
    const anchor = {
      getBoundingClientRect: () => ({
        top: 300 + anchorOffset - pane.scrollTop,
        height: 80,
      }),
    };
    const prose = {
      clientWidth: 700,
      closest: () => pane,
      contains: (element: unknown) => element === anchor,
      getBoundingClientRect: () => ({ top: 300 - pane.scrollTop }),
    } as unknown as HTMLElement;
    vi.stubGlobal("document", {
      getElementById: (id: string) => (id === "article-opening" ? anchor : null),
    });
    try {
      const origin = captureCitationPosition(
        { id: "article-opening", fraction: 0 },
        prose,
      );
      // Focusing the choice popup may scroll the pane before a reference is chosen.
      pane.scrollTop = 28;
      pane.scrollTop = 500;
      restoreReadingPosition(origin, prose, 200);
      expect(pane.scrollTop).toBe(0);

      // A changed width or earlier content means the old pixel offset is no longer safe.
      pane.clientWidth = 800;
      pane.scrollTop = 500;
      restoreReadingPosition(origin, prose, 200);
      expect(pane.scrollTop).toBe(160);
      pane.clientWidth = 900;
      anchorOffset = 90;
      pane.scrollTop = 500;
      restoreReadingPosition(origin, prose, 200);
      expect(pane.scrollTop).toBe(190);

      // Ordinary reading positions still use their fractional anchor.
      pane.scrollTop = 500;
      restoreReadingPosition({ id: "article-opening", fraction: 0.5 }, prose, 200);
      expect(pane.scrollTop).toBe(230);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

it("shows edition author and affiliation text when native metadata was consumed", () => {
  const body = block("body", "Body paragraph.");
  const pending = block("affiliation", "Additional institute");
  const r = report([body, pending], [item(body)]);
  r.reading!.information_items = {
    authors: [
      {
        ...item(body),
        id: "authors",
        parts: [],
        content: "Alice One, Bob Two; Example University; alice@example.org",
      },
    ],
  };
  r.reading!.information_parts = { authors: item(pending).parts };
  const byline = render(r).match(/<p class="article-byline">([\s\S]*?)<\/p>/)?.[1] || "";
  expect(byline).toContain("Alice One, Bob Two");
  expect(byline).toContain("Example University");
  expect(byline).toContain("alice@example.org");
  expect(byline).toContain("Additional institute");
});

it("keeps captions in the paper and cross-reference targets, outside chapter navigation", () => {
  const heading = block("section", "7.2 The corrected cubic");
  const fig = block(
    "figure",
    "Figure 2: The two bounding nodes. " + "Original caption. ".repeat(30),
  );
  const table = block("table", "Table 1: Notation used across the proof.");
  const r = report(
    [heading, fig, table],
    [
      { ...item(heading, "heading"), label: heading.text },
      { ...item(fig, "heading"), label: fig.text },
      { ...item(table, "table"), label: table.text },
    ],
  );
  const html = render(r);
  const nav = html.match(/<nav class="reading-outline"[\s\S]*?<\/nav>/)?.[0] || "";
  expect(nav).toContain(heading.text);
  expect(nav).not.toContain("Figure 2");
  expect(nav).not.toContain("Table 1");
  expect(html).toContain('id="reading-figure"');
  expect(html).toContain('id="block-figure"');
  expect(html).toContain('class="reading-object object-figure ');
  expect(html).toContain("The two bounding nodes.");
  expect(html).toContain("Notation used across the proof.");
  expect(r.reading!.items[1].kind).toBe("heading");
});

it("offers offline reading languages when generated translations are embedded", () => {
  const source = block("b1", "Attention may improve the result [1].");
  const r = report([source], [item(source)]);
  r.translations = {
    "reading-b1": { text: "注意力可能改善结果 [1]。", model: "fixture" },
  };
  const html = render(r);
  expect(html).toContain('aria-label="阅读语言"');
  expect(html).toContain("Attention may improve");
  expect(html).not.toContain("翻译此段");
});

describe("reading language segmented control", () => {
  it.each(["original", "bilingual", "zh"] as const)(
    "keeps all three choices and one tab stop when %s is selected",
    (language) => {
      const html = renderToStaticMarkup(
        <ReadingLanguageSwitch language={language} onChange={() => {}} />,
      );
      expect(html).toContain('class="segmented language-switch"');
      expect(html).toContain('role="radiogroup"');
      expect(html).toContain('aria-orientation="horizontal"');
      expect(html.match(/role="radio"/g)).toHaveLength(3);
      expect(html.match(/aria-checked="true"/g)).toHaveLength(1);
      expect(html.match(/tabindex="0"/g)).toHaveLength(1);
      expect(html.match(/tabindex="-1"/g)).toHaveLength(2);
      for (const label of ["原文", "对照", "译文"]) expect(html).toContain(label);
    },
  );

  it.each([
    ["original", "ArrowLeft", "zh", 2],
    ["zh", "ArrowRight", "original", 0],
    ["original", "ArrowDown", "bilingual", 1],
    ["zh", "ArrowUp", "bilingual", 1],
    ["bilingual", "Home", "original", 0],
    ["original", "End", "zh", 2],
  ] as const)("selects and focuses %s + %s -> %s", (language, key, next, index) => {
    const onChange = vi.fn();
    const focus = [vi.fn(), vi.fn(), vi.fn()];
    const event = {
      key,
      preventDefault: vi.fn(),
      stopPropagation: vi.fn(),
      currentTarget: { querySelectorAll: () => focus.map((focus) => ({ focus })) },
    };
    const element = ReadingLanguageSwitch({ language, onChange });
    element.props.onKeyDown(event);
    expect(onChange).toHaveBeenCalledExactlyOnceWith(next);
    expect(focus[index]).toHaveBeenCalledOnce();
    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(event.stopPropagation).toHaveBeenCalledOnce();
  });

  it("leaves native activation and browser modifier shortcuts unchanged", () => {
    const onChange = vi.fn();
    const element = ReadingLanguageSwitch({ language: "original", onChange });
    for (const values of [
      { key: " " },
      { key: "Enter" },
      { key: "Tab" },
      { key: "ArrowRight", altKey: true },
      { key: "Home", ctrlKey: true },
    ]) {
      const event = { ...values, preventDefault: vi.fn(), stopPropagation: vi.fn() };
      element.props.onKeyDown(event);
      expect(event.preventDefault).not.toHaveBeenCalled();
    }
    expect(onChange).not.toHaveBeenCalled();
    element.props.children[1].props.onClick();
    expect(onChange).toHaveBeenCalledExactlyOnceWith("bilingual");
  });
});

it("hides large or unnumbered reference indexes, keeping search and the entries", () => {
  const r = report([], []);
  r.reading!.references = Array.from({ length: 21 }, (_, n) => ({
    ...item(block(`ref-${n}`, `Author ${n}`), "reference"),
    content: `Author ${n}`,
    reference_number: String(n + 1),
  }));
  expect(render(r)).not.toContain('aria-label="参考文献索引"');
  expect(render(r)).toContain("搜索参考文献");
  expect(render(r)).toContain("Author 20");
  r.reading!.references = r.reading!.references.slice(0, 2);
  expect(render(r)).toContain('aria-label="参考文献索引"');
  r.reading!.references.forEach((ref) => {
    ref.reference_number = "";
    ref.label = "";
  });
  expect(render(r)).not.toContain('aria-label="参考文献索引"');
});

it("shows a compact citation without technical tails while linking the full original DOI", () => {
  const text = "Long Author, ".repeat(100) +
    "Phi-3 technical report. CoRR, 2024. doi:10.48550/ARXIV.2404.14219. " +
    "URL https://doi.org/10.48550/arXiv.2404.14219.";
  const reference = block("long-reference", text);
  const r = report([reference], []);
  r.reading!.references = [{ ...item(reference, "reference"), href: "" }];
  const html = render(r);
  const citation = html.match(/<details class="reference-citation[\s\S]*?<\/details>/)?.[0] || "";
  expect(citation).toContain("展开完整引文");
  expect(citation).toContain("Phi-3 technical report");
  expect(citation).toContain("CoRR, 2024");
  expect(citation).not.toContain("10.48550");
  expect(citation).not.toContain("https://");
  expect(html).toContain('href="https://doi.org/10.48550/arxiv.2404.14219"');
  expect(html).toContain("复制引用");
  expect(html).toContain('id="block-long-reference"');
  expect(reference.text).toBe(text);
});

it("offers body locations without requiring an earlier citation click", () => {
  const first = block("first", "Prior work [2–3].");
  const second = block("second", "Another mention [3].");
  const r = report([first, second], [item(first), item(second)]);
  r.reading!.references = [2, 3, 4].map((number) => ({
    ...item(block(`ref-${number}`, "Reference"), "reference"),
    id: `ref-${number}`,
    parts: [],
    reference_number: String(number),
    content: `Reference ${number}`,
  }));
  const html = render(r);
  expect(html).toContain("正文引用 · 1 处");
  expect(html).toContain("正文引用 · 2 处");
  expect(html).toContain("未找到正文引用");
  expect(html).not.toContain("返回引用</button>");
});

it("applies saved display terminology to translations and restores from untouched originals", () => {
  const source = block("b1", "We use dreaming to improve the simulator.");
  const r = report([source], [item(source)]);
  r.translations = {
    "reading-b1": { text: "在模拟器中进行梦想（dreaming）。", model: "fixture" },
    "@title": { text: "梦想方法", model: "fixture" },
  };
  let rules = [{ id: "dream", source: "梦想", replacement: "梦境模拟" }];
  vi.stubGlobal("localStorage", {
    getItem: (key: string) =>
      JSON.stringify(
        key === "paperlens:term-replacements"
          ? rules
          : key === "paperlens:reading-language"
            ? "bilingual"
            : null,
      ),
  });
  try {
    const changed = render(r);
    expect(changed).toContain("在模拟器中进行梦境模拟（dreaming）。");
    expect(changed).toContain("梦境模拟方法");
    expect(changed).toContain(source.text);
    expect(r.translations["reading-b1"].text).toBe("在模拟器中进行梦想（dreaming）。");
    rules = [];
    const restored = render(r);
    expect(restored).toContain("在模拟器中进行梦想（dreaming）。");
    expect(restored).not.toContain("梦境模拟");
  } finally {
    vi.unstubAllGlobals();
  }
});
const transcription: ReadingTranscription = {
  source: "vision",
  verified: false,
  warnings: ["需核对下标"],
  segments: [
    { text: "输入：x\n1. <script>literal</script> 更新 " },
    {
      text: "bad alternative",
      formula: {
        latex: "x_i^2",
        mathml: "",
        status: "renderable",
        source: "vision",
        verified: false,
        warnings: [],
      },
    },
    { text: "\n2. 返回" },
  ],
};

it("displays a cross-page listing as one frame and keeps both source item IDs", () => {
  const a = { ...block("a", "int main() {"), bbox: { ...bbox, y1: 0.9 } };
  const b = { ...block("b", "    return 0;\n}"), page: 2 };
  const c = { ...block("c", "print(1)"), page: 3 };
  const r = report(
    [a, b, c],
    [
      {
        ...item(a, "code"),
        bbox: a.bbox,
        content: a.text,
        code_language: "cpp",
        label: "Listing 1",
      },
      { ...item(b, "code"), page: 2, content: b.text },
      {
        ...item(c, "code"),
        page: 3,
        content: c.text,
        code_language: "python",
        label: "Listing 2",
      },
    ],
  );
  const html = render(r);
  expect(html.match(/class="reading-code-block"/g)).toHaveLength(2);
  expect(html.match(/aria-label="复制代码"/g)).toHaveLength(2);
  for (const id of ["reading-a", "reading-b", "block-a", "block-b"])
    expect(html.match(new RegExp(`id="${id}"`, "g"))).toHaveLength(1);
  expect(html).toContain("    ");
  expect(html).toContain('class="hljs-keyword">return');
});

describe("continuous reading transcription", () => {
  it("renders body TeX separately while preserving original prose and mark offsets", () => {
    const b = block("math-body", "😀前 $x_i$ 后一句");
    const r = report([b], [{ ...item(b), content: b.text }]);
    const html = render(r, "", [
      {
        id: "after",
        selection: { block_id: b.id, start: 10, end: 13, text: "后一句" },
        color: "yellow",
        note: "",
        created_at: "",
      } as Annotation,
    ]);
    expect(html).toContain('data-source-start="0" data-raw-text="true"');
    expect(html).toContain('data-source-start="9" data-raw-text="true"');
    expect(html).toContain("x_i");
    expect(html).not.toContain("$x_i$");
    expect(html).toContain('data-annotation="after"');
    expect(b.text).toBe("😀前 $x_i$ 后一句");
  });
  it("links citations in semantic content and legacy transcription using explicit reference numbers", () => {
    const body = block("body", "damaged paragraph");
    const reference = block("ref", "damaged reference");
    const r = report([body, reference], [{ ...item(body), content: "Faithful [2]." }]);
    r.reading!.references = [
      {
        ...item(reference, "reference"),
        label: "[99]",
        reference_number: "2",
        content: "Original bibliography",
      },
    ];
    const html = render(r);
    expect(html).toContain('data-cite="2"');
    expect(html).toContain("Original bibliography");
    expect(html).not.toContain("damaged paragraph");
    expect(html).not.toContain("damaged reference");
    expect(html).not.toContain("data-raw-text");
    expect(html).toContain('id="block-body"');
    expect(html).toContain('id="block-ref"');
    r.reading!.items = [
      {
        ...item(body),
        transcription: { ...transcription, segments: [{ text: "Legacy [2]." }] },
      },
    ];
    expect(render(r)).toContain('data-cite="2"');
  });

  it("retains exact source offsets and existing annotations when content matches", () => {
    const b = block("exact", "Read 😀 [2].");
    const r = report([b], [{ ...item(b), content: b.text }]);
    const mark = {
      id: "saved",
      selection: { block_id: b.id, start: 5, end: 7, text: "😀" },
      color: "blue",
      style: "underline",
      note: "Keep this",
      page: 1,
    } as Annotation;
    const html = render(r, "", [mark]);
    expect(html).toContain(
      'data-block-id="exact" data-source-start="0" data-raw-text="true"',
    );
    expect(html).toContain('data-annotation="saved"');
    expect(html).toContain("😀");
  });

  it("keeps source coordinates and saved marks only for exact runs within repaired content", () => {
    const b = block("runs", "原句😀 broken [2] source tail");
    const content = "原句😀 $x_i$ [2] repaired tail";
    const sourceCitation = b.text.indexOf("[2]");
    const contentCitation = content.indexOf("[2]");
    const r = report(
      [b],
      [
        {
          ...item(b),
          content,
          content_spans: [
            { start: 0, end: 5, source: { block_id: b.id, start: 0, end: 5, join: "" } },
            // A stale or incorrect run must never create false source coordinates.
            {
              start: 5,
              end: 10,
              source: { block_id: b.id, start: 5, end: 10, join: "" },
            },
            {
              start: contentCitation,
              end: contentCitation + 3,
              source: {
                block_id: b.id,
                start: sourceCitation,
                end: sourceCitation + 3,
                join: "",
              },
            },
          ],
        },
      ],
    );
    r.reading!.references = [
      {
        ...item(b, "reference"),
        id: "ref2",
        content: "Source paper",
        reference_number: "2",
        parts: [],
      },
    ];
    const mark = {
      id: "saved-run",
      selection: { block_id: b.id, start: 2, end: 4, text: "😀" },
      color: "blue",
      style: "underline",
      note: "Keep this",
      page: 1,
    } as Annotation;
    const html = render(r, "", [mark]);
    expect(html).toContain('data-annotation="saved-run"');
    expect(html).toContain('data-source-start="0"');
    expect(html).toContain(`data-source-start="${sourceCitation}"`);
    expect(html.match(/data-raw-text/g)).toHaveLength(2);
    expect(html).toContain('data-cite="2"');
    expect(html).toContain("x_i");
    expect(html).toContain("repaired tail");
    expect(html).not.toContain("broken");
    expect(html).not.toContain("source tail");
    expect(html.match(/id="block-runs"/g)).toHaveLength(1);
  });

  it("prefers structured table cells and renders structured end matter without raw text", () => {
    const b = block("table", "broken table text");
    b.table = {
      rows: 1,
      cols: 1,
      cells: [
        {
          text: "OLD CELL",
          row_start: 0,
          row_end: 1,
          col_start: 0,
          col_end: 1,
          column_header: false,
        },
      ],
    };
    const r = report(
      [b],
      [
        {
          ...item(b, "table"),
          label: "Table 1",
          content: "Original caption",
          table: {
            rows: 1,
            cols: 1,
            cells: [
              {
                text: "NEW CELL",
                row_start: 0,
                row_end: 1,
                col_start: 0,
                col_end: 1,
                column_header: true,
              },
            ],
          },
        },
      ],
    );
    r.reading!.information = { declarations: [b.id] };
    r.reading!.information_items = {
      declarations: [{ ...item(b), id: "funding", content: "Funded by original grant" }],
    };
    const html = render(r);
    expect(html).toContain("NEW CELL");
    expect(html).not.toContain("OLD CELL");
    expect(html).not.toContain("broken table text");
    expect(html).toContain("Original caption");
    expect(html.indexOf("Funded by original grant")).toBeGreaterThan(
      html.indexOf('id="article-information"'),
    );
    expect(html).not.toContain("article-legend");
    expect(html).not.toContain("原始提取文字");
    r.reading!.information_items.declarations = [];
    expect(render(r)).not.toContain("Funded by original grant");
    expect(render(r)).not.toContain("broken table text");
  });

  it("reads pure TeX content and keeps its formula number and source anchor", () => {
    const b = block("formula", "damaged formula");
    const html = render(
      report([b], [{ ...item(b, "formula"), content: "x_i^2", label: "(3)" }]),
    );
    expect(html).toContain("x_i^2");
    expect(html).toContain('aria-label="公式 3"');
    expect(html).not.toContain("<figcaption>");
    expect(html).toContain('id="block-formula"');
    expect(html).not.toContain("data-raw-text");
    expect(html).not.toContain("damaged formula");
  });

  it("defaults to book typography with a genuine toggle and marks only page boundaries", () => {
    const a = block("a", "First paragraph");
    const b = block("b", "Second paragraph");
    const html = render(report([a, b], [item(a), item(b)]));
    expect(html).toContain('data-reading-font="serif"');
    expect(html).toContain("--reading-size:18px");
    expect(html).toContain("--reading-measure:35em");
    expect(html).toContain('aria-pressed="false"');
    expect(html.match(/data-page-start="true"/g)).toHaveLength(1);
    expect(html).toContain('aria-controls="reading-typesetting"');
  });
  it("renders literal corrected text, preserved newlines and math without old selection offsets", () => {
    const b = block("corrected", "damaged ∑p");
    const html = render(report([b], [{ ...item(b), transcription }]), "输入");
    expect(html).not.toContain("识别转录");
    // A highlighted match is not selected until the user locates it.
    expect(html).toContain("0/1");
    expect(html).not.toContain("复制文字与 LaTeX");
    expect(html).toContain("\n1. &lt;script&gt;literal&lt;/script&gt;");
    expect(html).toContain("x_i^2");
    expect(html).not.toContain("bad alternative");
    expect(html).not.toContain("damaged ∑p");
    expect(html).not.toContain("data-raw-text");
    expect(html).not.toContain("data-source-start");
    expect(html).toContain('id="block-corrected"');
  });

  it("keeps reliable body offsets and removes algorithm raw extraction diagnostics", () => {
    const body = block("body", "正常正文 i=1");
    const algorithm = block("algorithm", "broken algorithm raw");
    const html = render(
      report(
        [body, algorithm],
        [item(body), { ...item(algorithm, "algorithm"), transcription }],
      ),
    );
    expect(html).toContain(
      'data-block-id="body" data-source-start="0" data-raw-text="true"',
    );
    const algorithmHtml = html.slice(
      html.indexOf('id="reading-algorithm"'),
      html.indexOf('id="article-information"'),
    );
    expect(algorithmHtml).not.toContain("原始提取文字（诊断）");
    expect(algorithmHtml).not.toContain("broken algorithm raw");
    expect(algorithmHtml).not.toContain("提取文字与勾画");
    expect(algorithmHtml).not.toContain("data-raw-text");
  });

  it("displays exact metadata slices instead of the complete mixed block", () => {
    const mixed = block("mixed", "基金：123。模型本身缺乏探索机制。");
    const r = report([mixed], []);
    r.reading!.information = { declarations: [mixed.id] };
    r.reading!.information_parts = {
      declarations: [
        { block_id: mixed.id, start: 0, end: 7, join: "", bbox: { ...bbox, x1: 0.4 } },
      ],
    };
    const html = render(r);
    expect(html).toContain("基金：123。");
    expect(html).not.toContain("模型本身缺乏探索机制");
    expect(html).toContain('data-source-start="0"');
  });

  it("offers the original page when formula recognition is unresolved", () => {
    const b = block("formula", "damaged formula");
    b.formula = {
      latex: "",
      mathml: "",
      status: "unresolved",
      source: "pdf_text",
      verified: false,
      warnings: [],
    };
    const html = render(report([b], [item(b, "formula")]));
    expect(html).toContain("请查看原页");
    expect(html).not.toContain("原始提取文字（诊断）");
    expect(html).not.toContain("data-raw-text");
  });
  it("links figure mentions to their figure and marks the page in the margin", () => {
    const fig = block("fig", "Figure 1: Overview");
    const body = block("body", "As Figure 1 shows, it works.");
    const figure = { ...item(fig, "figure"), label: "Figure 1: Overview" };
    const html = render(report([body, fig], [item(body), figure]));
    expect(html).toContain('data-xref="fig:1"');
    expect(html).toContain('aria-label="查看原页 PDF 1"');
    expect(html).toContain('class="caption-tag"');
    // Without a translation service there is no language switch.
    expect(html).not.toContain("阅读语言");
  });
  it("reserves an original image crop's dimensions before lazy loading", () => {
    const b = block("crop", "Figure 1");
    const r = report(
      [b],
      [{ ...item(b, "figure"), label: "Figure 1", asset: "data:image/png;base64,AA==" }],
    );
    r.document.pages[0].width = 100;
    r.document.pages[0].height = 200;
    const image = render(r).match(/<img[^>]*>/)?.[0] || "";
    for (const attribute of [
      'loading="lazy"',
      'width="160"',
      'height="40"',
      'style="height:auto"',
    ])
      expect(image).toContain(attribute);
  });
  it("reads algorithm content directly and keeps its image behind an optional comparison", () => {
    const b = block("algorithm", "damaged extraction");
    const html = render(
      report(
        [b],
        [
          {
            ...item(b, "algorithm"),
            label: "算法1",
            content: "算法1 完整算法\n1) 更新\n2) 返回",
            asset: "data:image/png;base64,AA==",
          },
        ],
      ),
    );
    expect(html).toContain(
      '<details class="object-original"><summary>算法原图</summary>',
    );
    expect(html.replace(/<[^>]+>/g, "")).toContain("算法1 完整算法\n1) 更新\n2) 返回");
    expect(html.match(/data-xref="alg:1"/g)).toHaveLength(1);
    expect(html).not.toContain("<figcaption>");
    expect(html).not.toContain("damaged extraction");
  });
});
