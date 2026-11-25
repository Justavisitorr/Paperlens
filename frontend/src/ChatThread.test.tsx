import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { Thread, RefChip } from "./ChatThread";
import type { ChatMessage, ChatRef, Session } from "./chat";

vi.hoisted(() => {
  (globalThis as { location?: unknown }).location = { protocol: "http:" };
});

const object: ChatRef = {
  kind: "reading_item",
  paper_id: "a",
  paper_title: "Paper",
  item_id: "eq-1",
  source_version: "version-1",
  label: "公式 (1)",
  text: "x_i^2 = y",
  page: 2,
};
const user: ChatMessage = {
  id: "question",
  role: "user",
  content: "Explain the equation",
  created_at: "2026-10-07",
  refs: [object],
  reply: null,
  error: null,
};
const answer: ChatMessage = {
  id: "answer",
  role: "assistant",
  content: "This is the final answer",
  created_at: "2026-10-07",
  refs: [],
  reply: { answer: "This is the final answer", grounded: true, quotes: [] },
  error: null,
};
function session(messages: ChatMessage[], pending: Session["pending"] = null): Session {
  return {
    doc: {
      id: "chat",
      title: "",
      created_at: "",
      updated_at: "",
      pinned: false,
      refs: [object],
      messages,
    },
    keep: false,
    saveError: "",
    pending,
  };
}
const render = (
  s: Session,
  onPin?: (answer: ChatMessage, question: ChatMessage | undefined) => void,
) =>
  renderToStaticMarkup(
    <Thread session={s} papers={[]} onRetry={() => {}} onStop={() => {}} onPin={onPin} />,
  );

describe("chat reading objects and margin action", () => {
  it("displays reading object identity and selected context without turning it into a passage", () => {
    const chip = renderToStaticMarkup(<RefChip value={object} />);
    expect(chip).toContain("kind-reading_item");
    expect(chip).toContain("公式 (1)");
    const html = render(session([user]));
    expect(html).toContain("公式 (1)");
    expect(html).toContain("x_i^2 = y");
  });

  it("offers pinning only with a callback and a completed grounded answer", () => {
    expect(render(session([user, answer]), () => {})).toContain("放到页边");
    expect(render(session([user, answer]))).not.toContain("放到页边");
    expect(
      render(
        session([user, { ...answer, reply: { ...answer.reply!, grounded: false } }]),
        () => {},
      ),
    ).not.toContain("放到页边");
    expect(render(session([user, { ...answer, reply: null }]), () => {})).not.toContain(
      "放到页边",
    );
    expect(
      render(session([user, { ...answer, error: "failed" }]), () => {}),
    ).not.toContain("放到页边");
    expect(render(session([user, { ...answer, content: "" }]), () => {})).not.toContain(
      "放到页边",
    );
  });

  it("keeps a provisional streaming answer separate from actions", () => {
    const html = render(
      session([user], {
        messageId: user.id,
        started: Date.now(),
        papers: 1,
        controller: new AbortController(),
        text: "Streaming draft",
      }),
      () => {},
    );
    expect(html).toContain("Streaming draft");
    expect(html).toContain("引文尚未核对");
    expect(html).not.toContain("放到页边");
    expect(html).not.toContain("导出回答");
  });
});
