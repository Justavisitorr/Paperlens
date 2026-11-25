import { describe, expect, it } from "vitest";
import {
  discoverTermCandidates,
  normalizeRules,
  replaceTranslationTerms,
  type TermReplacementRule,
} from "./termReplacements";

const rules = (...pairs: [string, string][]): TermReplacementRule[] =>
  pairs.map(([source, replacement], i) => ({ id: String(i), source, replacement }));

describe("translation display replacements", () => {
  it("replaces every occurrence and restores the unchanged source by removing rules", () => {
    const original = "通过梦想（dreaming）进行梦想。";
    const selected = rules(["梦想", "梦境模拟"]);
    expect(replaceTranslationTerms(original, selected)).toBe(
      "通过梦境模拟（dreaming）进行梦境模拟。",
    );
    expect(replaceTranslationTerms(original, [])).toBe(original);
    expect(original).toBe("通过梦想（dreaming）进行梦想。");
    expect(selected[0].source).toBe("梦想");
  });

  it("uses longest matches without cascading newly inserted text", () => {
    expect(
      replaceTranslationTerms(
        "世界模型 世界 模型",
        rules(["世界", "天地"], ["世界模型", "世界"], ["模型", "世界模型"]),
      ),
    ).toBe("世界 天地 世界模型");
  });

  it("does not replace Latin terms inside words, numbers or identifiers", () => {
    expect(
      replaceTranslationTerms(
        "world worldwide underworld world2 _world worldé 中文world世界 (world)",
        rules(["world", "世界"]),
      ),
    ).toBe("世界 worldwide underworld world2 _world worldé 中文世界世界 (世界)");
  });

  it("treats regex metacharacters and replacement dollar syntax literally", () => {
    expect(
      replaceTranslationTerms(
        "C++ a.b axb [x]",
        rules(["C++", "$&"], ["a.b", "$1"], ["[x]", "$`"]),
      ),
    ).toBe("$& $1 axb $`");
  });

  it("preserves all supported TeX delimiters and rejects matches across protected spans", () => {
    const source = String.raw`world $world$ $$world$$ \(world\) \[world\] world`;
    expect(replaceTranslationTerms(source, rules(["world", "世界"]))).toBe(
      String.raw`世界 $world$ $$world$$ \(world\) \[world\] 世界`,
    );
    expect(replaceTranslationTerms("a $x$ b", rules(["a $x$ b", "oops"]))).toBe(
      "a $x$ b",
    );
  });

  it("protects inline code, both fence styles, and an unclosed code fence", () => {
    const source =
      "world `world` ``world ` world``\n```ts\nworld\n```\nworld\n~~~\nworld\n~~~\nworld\n```\nworld";
    expect(replaceTranslationTerms(source, rules(["world", "世界"]))).toBe(
      "世界 `world` ``world ` world``\n```ts\nworld\n```\n世界\n~~~\nworld\n~~~\n世界\n```\nworld",
    );
  });

  it("does not let dollar signs inside code consume surrounding prose", () => {
    expect(replaceTranslationTerms("`$` world `$`", rules(["world", "世界"]))).toBe(
      "`$` 世界 `$`",
    );
  });

  it("changes link labels while preserving destinations and reference ids", () => {
    const source =
      '[world](https://world.test/world(foo) "world") [world][world]\n[world]: https://world.test\n<https://world.test> world';
    expect(replaceTranslationTerms(source, rules(["world", "世界"]))).toBe(
      '[世界](https://world.test/world(foo) "world") [世界][world]\n[world]: https://world.test\n<https://world.test> 世界',
    );
  });

  it("returns replacement strings as text without HTML or regex evaluation", () => {
    const unsafe = '<img src=x onerror="alert(1)">';
    expect(replaceTranslationTerms("世界", rules(["世界", unsafe]))).toBe(unsafe);
  });

  it("keeps parentheses inside quoted link titles and angle destinations intact", () => {
    const source = '[world](<https://world.test/path)> "world) world") world';
    expect(replaceTranslationTerms(source, rules(["world", "世界"]))).toBe(
      '[世界](<https://world.test/path)> "world) world") 世界',
    );
  });
});

describe("stored replacement validation", () => {
  it.each([null, undefined, {}, "[]", 42])(
    "rejects invalid top-level data %s",
    (value) => {
      expect(normalizeRules(value)).toEqual([]);
    },
  );
  it("drops malformed/no-op/duplicate rules and strips unknown fields", () => {
    expect(
      normalizeRules([
        null,
        7,
        { id: "bad", source: 4, replacement: "x" },
        { id: "empty", source: " ", replacement: "x" },
        { id: "noop", source: "x", replacement: "x" },
        { id: " 1 ", source: " 梦想 ", replacement: " 梦境模拟 ", dangerous: true },
        { id: "2", source: "梦想", replacement: "other" },
        { id: "1", source: "world", replacement: "other" },
      ]),
    ).toEqual([{ id: "1", source: "梦想", replacement: "梦境模拟" }]);
  });
});

describe("replacement suggestions", () => {
  it("splits explicit spaced-slash glossary aliases, preserving unspaced slashes", () => {
    expect(
      discoverTermCandidates(
        [{ term: "回放模拟器 / 世界" }, { term: "模型 ／ 仿真器" }, { term: "I/O" }],
        { a: { text: "回放模拟器中的世界、模型与 I/O" } },
      ),
    ).toEqual([
      { source: "回放模拟器", matched: true, glossaryTerms: ["回放模拟器 / 世界"] },
      { source: "世界", matched: true, glossaryTerms: ["回放模拟器 / 世界"] },
      { source: "模型", matched: true, glossaryTerms: ["模型 ／ 仿真器"] },
      { source: "仿真器", matched: false, glossaryTerms: ["模型 ／ 仿真器"] },
      { source: "I/O", matched: true, glossaryTerms: ["I/O"] },
    ]);
  });

  it("keeps every explicitly paired glossary term, marking available source matches", () => {
    const glossary = [
      { term: "递归自我改进 (Recursive Self-Improvement)" },
      { term: "world (世界)" },
      { term: "unused (未用)" },
    ];
    expect(
      discoverTermCandidates(glossary, { a: { text: "递归自我改进与世界" } }),
    ).toEqual([
      {
        source: "递归自我改进",
        term: "Recursive Self-Improvement",
        matched: true,
        glossaryTerms: [glossary[0].term],
      },
      { source: "世界", term: "world", matched: true, glossaryTerms: [glossary[1].term] },
      {
        source: "未用",
        term: "unused",
        matched: false,
        glossaryTerms: [glossary[2].term],
      },
    ]);
  });
  it("includes untranslated glossary terms and explicit English-first pairs", () => {
    expect(
      discoverTermCandidates([{ term: "dreaming" }], { a: { text: "dreaming（梦想）" } }),
    ).toEqual([
      { source: "dreaming", matched: true, glossaryTerms: ["dreaming"] },
      { source: "梦想", term: "dreaming", matched: true, glossaryTerms: ["dreaming"] },
    ]);
  });
  it("does not guess a Chinese translation by slicing the preceding clause", () => {
    expect(
      discoverTermCandidates([{ term: "dreaming" }], {
        a: { text: "通过在这个模拟器中进行梦想（dreaming）。" },
      }),
    ).toEqual([{ source: "dreaming", matched: true, glossaryTerms: ["dreaming"] }]);
  });
  it("retains glossary terms but does not count code/math or longer words as a match", () => {
    expect(
      discoverTermCandidates([{ term: "world" }], {
        a: { text: "$world$ `world` worldwide" },
      }),
    ).toEqual([{ source: "world", matched: false, glossaryTerms: ["world"] }]);
  });

  it("covers every glossary entry when no translations exist, including symbols and long labels", () => {
    const longLabel = "Long unfamiliar technical label ".repeat(5).trim();
    const glossary = [
      { term: "dreaming" },
      { term: "世界模型 (world model)" },
      { term: "β-divergence" },
      { term: "λ（学习率）" },
      { term: "∇θ" },
      { term: longLabel },
      { term: "a / b" },
      { term: " / " },
      { term: "  带空格的术语  " },
      { term: "" },
      { term: "  " },
    ];
    const candidates = discoverTermCandidates(glossary, {});
    expect(candidates.every((candidate) => !candidate.matched)).toBe(true);
    expect(new Set(candidates.flatMap((candidate) => candidate.glossaryTerms))).toEqual(
      new Set(glossary.filter(({ term }) => term.trim()).map(({ term }) => term)),
    );
    expect(candidates.map(({ source }) => source)).toEqual([
      "dreaming",
      "世界模型",
      "β-divergence",
      "λ（学习率）",
      "∇θ",
      longLabel,
      "a",
      "b",
      "/",
      "带空格的术语",
    ]);
  });

  it("retains differing model wording without guessing a synonym or requiring a saved rule", () => {
    expect(
      discoverTermCandidates([{ term: "梦境模拟 (dreaming)" }, { term: "世界模型" }], {
        a: { text: "模型在梦想（dreaming）中继续改进。" },
      }),
    ).toEqual([
      {
        source: "梦境模拟",
        term: "dreaming",
        matched: false,
        glossaryTerms: ["梦境模拟 (dreaming)"],
      },
      { source: "dreaming", matched: true, glossaryTerms: ["梦境模拟 (dreaming)"] },
      { source: "世界模型", matched: false, glossaryTerms: ["世界模型"] },
    ]);
  });

  it("merges full glossary provenance when distinct entries share an alias", () => {
    const glossary = [
      { term: "世界" },
      { term: "回放模拟器 / 世界" },
      { term: "world (世界)" },
      { term: "世界" },
    ];
    const candidates = discoverTermCandidates(glossary, { a: { text: "世界与 world" } });
    expect(candidates.find(({ source }) => source === "世界")).toEqual({
      source: "世界",
      term: "world",
      matched: true,
      glossaryTerms: ["世界", "回放模拟器 / 世界", "world (世界)"],
    });
    expect(candidates.filter(({ source }) => source === "世界")).toHaveLength(1);
    expect(new Set(candidates.flatMap((candidate) => candidate.glossaryTerms))).toEqual(
      new Set(glossary.map(({ term }) => term)),
    );
  });

  it("does not supplement candidates from an English-first pair inside protected code", () => {
    expect(
      discoverTermCandidates([{ term: "dreaming" }], {
        a: { text: "`dreaming（梦想）`" },
      }),
    ).toEqual([{ source: "dreaming", matched: false, glossaryTerms: ["dreaming"] }]);
  });

  it("derives coverage solely from the current glossary, ignoring unrelated translated terms", () => {
    expect(
      discoverTermCandidates([{ term: "当前论文术语" }], {
        oldPaper: { text: "unrelated（其他论文已保存术语）" },
      }),
    ).toEqual([
      {
        source: "当前论文术语",
        matched: false,
        glossaryTerms: ["当前论文术语"],
      },
    ]);
  });
});
