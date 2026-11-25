import { describe, expect, it } from "vitest";
import { matchesReference } from "./ReferenceSearch";

describe("reference search", () => {
  const option = {
    id: "ref7",
    label: "[7]",
    text: "A. Novikov, N. Vũ. AlphaEvolve: A coding agent. 2025.",
  };
  it("matches multiple keywords in any order without case or accent barriers", () => {
    expect(matchesReference(option, "2025 alphaevolve vu")).toBe(true);
    expect(matchesReference(option, "[7] novikov")).toBe(true);
    expect(matchesReference(option, "  ")).toBe(true);
  });
  it("requires every keyword so an unrelated author/year does not match", () => {
    expect(matchesReference(option, "Novikov 2026")).toBe(false);
    expect(matchesReference(option, "Merrill")).toBe(false);
  });
});
