import { describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  // api.ts reads the page protocol when it loads; nothing here calls the service.
  (globalThis as { location?: unknown }).location = { protocol: "file:" };
});

import {
  LIMITS,
  addRef,
  answerMarkdown,
  blankConversation,
  configureChat,
  conversationMarkdown,
  discard,
  ensureSession,
  getSession,
  historyOf,
  removeRef,
  requestFor,
  send,
  titleFrom,
  type ChatMessage,
  type ChatRef,
} from "./chat";
import { segments } from "./annotations";
import { parseInline, parseMarkdown } from "./markdown";

const paper = (id: string): ChatRef => ({
  kind: "paper",
  paper_id: id,
  title: `P ${id}`,
});
const passage = (id: string, block: string): ChatRef => ({
  kind: "passage",
  paper_id: id,
  paper_title: `P ${id}`,
  block_id: block,
  start: 0,
  end: 4,
  text: "text",
});
const term = (name: string, paperId?: string): ChatRef => ({
  kind: "term",
  term_id: name,
  term: name,
  explanation: "e",
  note: "",
  paper_id: paperId,
  paper_title: "",
});
const readingItem = (id: string, item = "formula-1", version = "source-1"): ChatRef => ({
  kind: "reading_item",
  paper_id: id,
  paper_title: `P ${id}`,
  item_id: item,
  source_version: version,
  label: "公式 (1)",
  text: "x_i^2 = y",
  page: 2,
});
const msg = (
  role: "user" | "assistant",
  content: string,
  error?: string,
): ChatMessage => ({
  id: Math.random().toString(16).slice(2),
  role,
  content,
  created_at: "",
  refs: [],
  reply: null,
  error: error ?? null,
});

describe("chat references", () => {
  it("includes an object's paper, rotates objects independently, and replaces stale versions", () => {
    let refs = addRef([], passage("a", "b1")).refs;
    for (let i = 0; i < LIMITS.reading_items + 1; i++)
      refs = addRef(refs, readingItem("a", `formula-${i}`)).refs;
    expect(refs.filter((r) => r.kind === "reading_item")).toHaveLength(6);
    expect(refs.filter((r) => r.kind === "passage")).toEqual([passage("a", "b1")]);
    expect(refs.some((r) => r.kind === "reading_item" && r.item_id === "formula-0")).toBe(
      false,
    );
    refs = addRef(refs, readingItem("a", "formula-1", "source-2")).refs;
    const same = refs.filter(
      (r) => r.kind === "reading_item" && r.item_id === "formula-1",
    );
    expect(same).toEqual([readingItem("a", "formula-1", "source-2")]);
    expect(addRef(refs, readingItem("a", "formula-1", "source-2")).refs).toBe(refs);
    expect(addRef([], readingItem("b")).refs).toEqual([paper("b"), readingItem("b")]);
  });

  it("removes reading objects together with a paper, without changing other references", () => {
    const refs = [
      paper("a"),
      readingItem("a"),
      passage("a", "b1"),
      paper("b"),
      readingItem("b"),
    ];
    expect(removeRef(refs, paper("a"))).toEqual([paper("b"), readingItem("b")]);
  });
  it("brings a passage's paper along and ignores duplicates", () => {
    const { refs } = addRef([], passage("a", "b1"));
    expect(refs.map((r) => r.kind)).toEqual(["paper", "passage"]);
    expect(addRef(refs, passage("a", "b1")).refs).toBe(refs);
  });

  it("refuses a seventh paper but rotates passages", () => {
    let refs: ChatRef[] = [];
    for (let i = 0; i < LIMITS.papers; i++) refs = addRef(refs, paper(`p${i}`)).refs;
    const over = addRef(refs, paper("extra"));
    expect(over.error).toMatch(/最多/);
    expect(over.refs).toBe(refs);

    let quoted: ChatRef[] = [];
    for (let i = 0; i < LIMITS.passages + 1; i++)
      quoted = addRef(quoted, passage("a", `b${i}`)).refs;
    const blocks = quoted.flatMap((r) => (r.kind === "passage" ? [r.block_id] : []));
    expect(blocks).toHaveLength(LIMITS.passages);
    expect(blocks[0]).toBe("b1");
  });

  it("drops a paper's passages with the paper", () => {
    const refs = [paper("a"), passage("a", "b1"), paper("c"), passage("c", "b2")];
    expect(removeRef(refs, paper("a"))).toEqual([paper("c"), passage("c", "b2")]);
  });
});

describe("chat request", () => {
  it("sends only object identity and source version, while preserving exact passages", () => {
    const history = [{ role: "user" as const, content: "previous question" }];
    const body = requestFor([readingItem("a"), passage("a", "b1")], "explain", history);
    expect(body).toEqual({
      question: "explain",
      history,
      papers: ["a"],
      reading_items: [
        { paper_id: "a", item_id: "formula-1", source_version: "source-1" },
      ],
      passages: [{ paper_id: "a", block_id: "b1", start: 0, end: 4, text: "text" }],
      terms: [],
    });
    expect(requestFor([paper("a")], "q", [])).not.toHaveProperty("reading_items");
    expect(requestFor([readingItem("gone")], "q", [], new Set(["a"]))).not.toHaveProperty(
      "reading_items",
    );
    // A cached old version remains an old version; the server must reject it.
    expect(
      requestFor([readingItem("a", "formula-1", "stale")], "q", []).reading_items?.[0]
        .source_version,
    ).toBe("stale");
  });

  it("exports the selected object with its label, preserving formulas as source text", () => {
    const question = { ...msg("user", "What does this mean?"), refs: [readingItem("a")] };
    expect(
      conversationMarkdown({ ...blankConversation(), messages: [question] }),
    ).toContain("公式 (1)（P a P2）：x_i^2 = y");
  });

  it("preserves multiline sources and warnings in both answer and conversation exports", () => {
    const question = {
      ...msg("user", "Explain"),
      refs: [
        { ...passage("a", "b"), text: "原文\r\n\r\n## 下一段" },
        { ...readingItem("a"), text: "x_i^2\r\n\r\n第二行" },
      ],
    };
    const answer = {
      ...msg("assistant", "正文回答 [12]"),
      reply: {
        answer: "正文回答 [12]",
        papers: [{ paper_id: "a", ref: "A", title: "来源论文" }],
        quotes: [
          {
            n: 12,
            paper_id: "a",
            block_id: "b",
            page: 3,
            quote: "依据\r\n\r\n- 第二段\r末行",
          },
        ],
        warnings: ["尚待核对\r\n\r\n原始数据"],
      },
    };
    const single = answerMarkdown(answer);
    expect(single).toContain("正文回答 [12]");
    expect(single).toContain("12. 《来源论文》P3：“依据\n    \n    - 第二段\n    末行”");
    expect(single).toContain("> 提示：尚待核对\n> \n> 原始数据");
    const full = conversationMarkdown({
      ...blankConversation(),
      messages: [question, answer],
    });
    expect(full).toContain(single);
    expect(full).toContain("> 原文（P a）：原文\n> \n> ## 下一段");
    expect(full).toContain("> 公式 (1)（P a P2）：x_i^2\n> \n> 第二行");
    expect(full).not.toContain("\r");
  });

  it("restores browser quick drafts with object identities intact", () => {
    const saved = {
      ...blankConversation([readingItem("a")]),
      messages: [msg("user", "cached question")],
    };
    vi.stubGlobal("localStorage", {
      getItem: () => JSON.stringify(saved),
      removeItem: () => {},
    });
    const key = "quick:test-reading-object";
    expect(ensureSession(key, blankConversation, false).doc).toEqual(saved);
    discard(key);
    vi.unstubAllGlobals();
  });

  it("reports unsupported object refs on a legacy service before sending a request", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    configureChat({ multi: false, stream: false, library: new Set(["a"]) });
    const key = "test-legacy-reading-item";
    ensureSession(key, () => blankConversation([readingItem("a")]), false);
    send(key, "explain");
    await vi.waitFor(() => expect(getSession(key)?.pending).toBeNull());
    expect(getSession(key)?.doc.messages.at(-1)?.error).toMatch(/不支持引用公式/);
    expect(fetch).not.toHaveBeenCalled();
    discard(key);
    configureChat({ library: null });
    vi.unstubAllGlobals();
  });
  it("keeps only answered turns, clipped and capped", () => {
    const long = "x".repeat(LIMITS.turn + 50);
    const messages = [
      msg("user", "q1"),
      msg("assistant", "a1"),
      msg("user", "failed"),
      msg("assistant", "", "timeout"),
      msg("user", long),
      msg("assistant", "a3"),
      msg("user", "unanswered"),
    ];
    const history = historyOf(messages);
    expect(history.map((h) => h.content.slice(0, 2))).toEqual(["q1", "a1", "xx", "a3"]);
    expect(history[2].content).toHaveLength(LIMITS.turn);
    const many = Array.from({ length: 20 }, (_, i) =>
      msg(i % 2 ? "assistant" : "user", `${i}`),
    );
    expect(historyOf(many)).toHaveLength(LIMITS.history);
    expect(historyOf(many)[0].role).toBe("user");
  });

  it("skips papers no longer in the library", () => {
    const refs = [
      paper("a"),
      passage("a", "b1"),
      paper("gone"),
      passage("gone", "b2"),
      term("t", "gone"),
      term("u", "a"),
    ];
    const body = requestFor(refs, "q", [], new Set(["a"]));
    expect(body.papers).toEqual(["a"]);
    expect(body.passages.map((p) => p.block_id)).toEqual(["b1"]);
    expect(body.terms).toEqual([
      { term: "t", explanation: "e", note: "" },
      { term: "u", explanation: "e", note: "", paper_id: "a" },
    ]);
  });

  it("titles a conversation from its first question", () => {
    expect(titleFrom("  what\n is   this? ")).toBe("what is this?");
    expect(titleFrom("a".repeat(40))).toHaveLength(29);
  });
});

describe("model routing", () => {
  it.each([
    ["workspace-model-test", true, "conversation"],
    ["quick:model-test", true, "chat"],
    ["legacy-model-test", false, undefined],
  ] as const)(
    "routes %s without changing older service payloads",
    async (key, routing, role) => {
      const fetch = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            type: "done",
            reply: { answer: "reply", quotes: [], grounded: false },
          }) + "\n",
          { headers: { "Content-Type": "application/x-ndjson" } },
        ),
      );
      vi.stubGlobal("fetch", fetch);
      configureChat({ multi: true, stream: true, routing, library: new Set(["a"]) });
      try {
        ensureSession(key, () => blankConversation([paper("a")]), false);
        send(key, "question");
        await vi.waitFor(() => expect(getSession(key)?.pending).toBeNull());
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(JSON.parse(fetch.mock.calls[0][1].body).model_role).toBe(role);
        expect(getSession(key)?.doc.messages.at(-1)?.error).toBeNull();
      } finally {
        discard(key);
        configureChat({ multi: false, stream: false, routing: false, library: null });
        vi.unstubAllGlobals();
      }
    },
  );
});

describe("markdown", () => {
  it("parses the blocks the assistant writes", () => {
    const md =
      "## 方法\n\n- 一\n  续行\n- 二\n\n1. 甲\n\n> 引\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode\n```\n段落\n第二行";
    expect(parseMarkdown(md)).toEqual([
      { t: "h", level: 1, v: "方法" },
      { t: "ul", items: ["一 续行", "二"] },
      { t: "ol", items: ["甲"] },
      { t: "quote", lines: ["引"] },
      { t: "table", head: ["a", "b"], rows: [["1", "2"]] },
      { t: "code", v: "code" },
      { t: "p", lines: ["段落", "第二行"] },
    ]);
  });

  it("merges adjacent citation markers", () => {
    expect(parseInline("见 [1][2] 与 [3，4]。**重点** `x`")).toEqual([
      { t: "text", v: "见 " },
      { t: "cite", n: [1, 2] },
      { t: "text", v: " 与 " },
      { t: "cite", n: [3, 4] },
      { t: "text", v: "。" },
      { t: "strong", v: "重点" },
      { t: "text", v: " " },
      { t: "code", v: "x" },
    ]);
  });
});

describe("annotation segments", () => {
  const text = "The quick brown fox jumps";
  const join = (s: { text: string }[]) => s.map((p) => p.text).join("");

  it("keeps the text intact around highlights and a search hit", () => {
    const out = segments(text, "brown", [{ id: "m", start: 4, end: 15 }]);
    expect(join(out)).toBe(text);
    expect(out).toEqual([
      { text: "The ", hit: false, mark: null },
      { text: "quick ", hit: false, mark: "m" },
      { text: "brown", hit: true, mark: "m" },
      { text: " fox jumps", hit: false, mark: null },
    ]);
  });

  it("gives an overlap to the earlier highlight and ignores bad ranges", () => {
    const out = segments(text, "", [
      { id: "b", start: 8, end: 14 },
      { id: "a", start: 4, end: 10 },
      { id: "x", start: 40, end: 50 },
      { id: "y", start: 5, end: 5 },
    ]);
    expect(join(out)).toBe(text);
    expect(out.filter((p) => p.mark).map((p) => [p.text, p.mark])).toEqual([
      ["quick ", "a"],
      ["brow", "b"],
    ]);
  });
});
