import { beforeEach, describe, expect, it, vi } from "vitest";

const store = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
});

const terms = await import("./terms");
const read = () => JSON.parse(store.get("paperlens.terms") || "[]");
const base = {
  explanation: "",
  paperId: "p1",
  paperTitle: "BERT",
  origin: "glossary" as const,
};

describe("term library", () => {
  beforeEach(() => {
    terms.removeTerms(read().map((t: { id: string }) => t.id));
  });

  it("keys a term per paper, ignoring case and spaces", () => {
    terms.saveTerm({ ...base, term: " Transformer " });
    terms.saveTerm({ ...base, term: "transformer", paperId: "p2" });
    expect(read().map((t: { id: string }) => t.id)).toEqual([
      "p2::transformer",
      "p1::transformer",
    ]);
  });

  it("re-adding keeps the note and an earlier explanation", () => {
    const first = terms.saveTerm({ ...base, term: "MLM", explanation: "掩码语言模型" });
    terms.updateTerm(first.id, { note: "像完形填空" });
    terms.saveTerm({ ...base, term: "MLM", origin: "selection" });
    expect(read()).toHaveLength(1);
    expect(read()[0]).toMatchObject({ note: "像完形填空", explanation: "掩码语言模型" });
  });

  it("restores removed terms in their original order", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-01"));
    const a = terms.saveTerm({ ...base, term: "a" });
    vi.setSystemTime(new Date("2026-09-02"));
    terms.saveTerm({ ...base, term: "b" });
    vi.useRealTimers();
    terms.removeTerms([a.id]);
    terms.restoreTerms([a]);
    expect(read().map((t: { term: string }) => t.term)).toEqual(["b", "a"]);
  });

  it("keeps offline papers apart and routes them to the local reader", () => {
    expect(terms.paperKey("local", "BERT")).toBe("local:BERT");
    expect(terms.paperKey("abc", "BERT")).toBe("abc");
    expect(terms.routePaperId("local:BERT")).toBe("local");
  });

  it("exports CSV that spreadsheets read as UTF-8, quotes escaped", () => {
    const csv = terms.termsCsv([
      { ...base, id: "x", term: 'say "hi", ok', added: "2026-09-27T00:00:00Z" },
    ]);
    expect(csv.charCodeAt(0)).toBe(0xfeff);
    expect(csv).toContain('"say ""hi"", ok"');
  });

  it("keeps multiline Chinese CSV fields in their original columns", () => {
    const csv = terms.termsCsv([
      {
        ...base,
        id: "csv-lines",
        term: "注意力,机制",
        explanation: '第一行\r\n第二行含"引号"',
        note: "笔记\n\n下一段",
        page: 3,
        quote: "来源,原文",
        added: "2026-10-08T00:00:00Z",
      },
    ]);
    expect(csv).toBe(
      '\uFEFF"术语","解释","笔记","论文","页码","原文","加入时间"\r\n' +
        '"注意力,机制","第一行\r\n第二行含""引号""","笔记\n\n下一段","BERT","3","来源,原文","2026-10-08"',
    );
  });

  it("keeps every line of a note and its source in the Markdown quote", () => {
    const md = terms.termsMarkdown([
      {
        ...base,
        id: "md-lines",
        term: "attention",
        note: "第一行\r\n\r\n- 第二段",
        quote: "x_i^2\r\n\r\n## 原文标题\r最后一行",
        page: 3,
        added: "2026-10-08T00:00:00Z",
      },
    ]);
    expect(md).toContain("> 笔记：第一行\n> \n> - 第二段");
    expect(md).toContain("> 原文（P3）：x_i^2\n> \n> ## 原文标题\n> 最后一行");
    expect(md).not.toContain("\r");
  });

  it("hands a pending jump to the matching view only once", () => {
    terms.jumpLater("glossary", "MLM");
    expect(terms.takeJump("text")).toBeNull();
    expect(terms.takeJump("glossary")).toBe("MLM");
    expect(terms.takeJump("glossary")).toBeNull();
  });

  it("a written note leaves the explanation pending, never hidden", () => {
    const t = terms.saveTerm({ ...base, term: "KV cache", origin: "selection" });
    terms.updateTerm(t.id, { note: "  缓存 $K,V$ " });
    expect(read()[0]).toMatchObject({ note: "  缓存 $K,V$ ", explanation: "" });
    terms.updateTerm(t.id, { note: "   " });
    expect(read()[0].note).toBeUndefined();
    expect(read()[0].updated).toBeTruthy();
  });

  it("records who wrote an explanation and clears it with the text", () => {
    const t = terms.saveTerm({ ...base, term: "LoRA", explanation: "低秩适配" });
    expect(read()[0].explanationBy).toBe("paper");
    terms.updateTerm(t.id, { explanation: "我的话", explanationBy: "me" });
    terms.saveTerm({ ...base, term: "LoRA", explanation: "新的术语表解释" });
    expect(read()[0]).toMatchObject({ explanation: "我的话", explanationBy: "me" });
    terms.updateTerm(t.id, { explanation: " " });
    expect(read()[0]).toMatchObject({ explanation: "" });
    expect(read()[0].explanationBy).toBeUndefined();
  });

  it("an arriving AI answer fills only an empty explanation", () => {
    const t = terms.saveTerm({ ...base, term: "RoPE", origin: "selection" });
    terms.fillExplanation(t.id, "旋转位置编码", "ai", "m1");
    expect(read()[0]).toMatchObject({
      explanation: "旋转位置编码",
      explanationBy: "ai",
      explanationModel: "m1",
    });
    terms.fillExplanation(t.id, "另一个答案", "ai");
    expect(read()[0].explanation).toBe("旋转位置编码");
    terms.fillExplanation("missing", "x", "ai");
    expect(read()).toHaveLength(1);
  });

  it("a picked word prefers the paper glossary, then an AI answer", () => {
    const glossary = [{ term: "Softmax ", explanation: "归一化指数" }];
    expect(terms.pickedExplanation(glossary, " softmax", { answer: "AI" })).toEqual({
      explanation: "归一化指数",
      explanationBy: "paper",
    });
    expect(
      terms.pickedExplanation(glossary, "ReLU", { answer: "整流", model: "m" }),
    ).toEqual({
      explanation: "整流",
      explanationBy: "ai",
      explanationModel: "m",
    });
    expect(terms.pickedExplanation(glossary, "ReLU", { answer: " " })).toEqual({
      explanation: "",
    });
  });

  it("asks for an explanation in context, with the quote when kept", () => {
    const q = terms.explainQuestion({
      term: "MLM",
      paperTitle: "BERT",
      quote: "masked LM",
    });
    expect(q).toContain("《BERT》");
    expect(q).toContain("“MLM”");
    expect(q.split("\n")).toHaveLength(3);
    expect(
      terms.explainQuestion({ term: "MLM", paperTitle: "BERT" }).split("\n"),
    ).toHaveLength(2);
  });

  it("repairs stored records from older builds and other tabs", () => {
    const list = terms.normalizeTerms([
      { term: " GELU ", paperId: "p1", explanation: "", note: " " },
      {
        id: "p1::gelu",
        term: "GELU",
        paperId: "p1",
        explanation: "高斯误差线性单元",
        note: "平滑",
      },
      { term: "", paperId: "p1" },
      null,
      "junk",
    ]);
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({
      id: "p1::gelu",
      term: "GELU",
      paperTitle: "",
      explanation: "高斯误差线性单元",
      note: "平滑",
      origin: "selection",
    });
  });

  it("labels the explanation source in the Markdown export", () => {
    const md = terms.termsMarkdown([
      {
        ...base,
        id: "a",
        term: "x",
        explanation: "定义",
        explanationBy: "ai",
        added: "2026-10-08T00:00:00Z",
      },
    ]);
    expect(md).toContain("定义");
    expect(md).toContain("AI 解释");
  });
});
