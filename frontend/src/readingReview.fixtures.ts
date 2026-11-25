import type { DashboardReport } from "./types";

export function reviewReport(): DashboardReport {
  return {
    document: {
      title: "Paper",
      sha256: "source",
      warnings: [],
      pages: [{ number: 2, status: "ok", warnings: [] }],
      blocks: [
        { id: "b2", page: 2, kind: "text", bbox: { x0: 0.1, y0: 0.2, x1: 0.8, y1: 0.4 } },
      ],
    },
    analysis: {
      title: "Paper",
      summary: "Existing summary",
      research_question: "Existing question",
      reading_guide: ["Read methods"],
      warnings: [],
      nodes: [
        {
          id: "n2",
          title: "Limited conclusion",
          kind: "contribution",
          summary: "Existing contribution",
          support: "partial",
          evidence_ids: ["e2"],
        },
        {
          id: "n3",
          title: "Boundary",
          summary: "Existing limit",
          kind: "limitation",
          support: "supported",
          evidence_ids: ["e2"],
        },
      ],
    },
    evidence: [
      {
        id: "e2",
        block_id: "b2",
        page: 2,
        quote: "Held-out test split",
        bbox: { x0: 0.1, y0: 0.2, x1: 0.8, y1: 0.4 },
      },
    ],
    quantitative_results: [
      {
        id: "q2",
        metric: "Accuracy",
        method: "Method A",
        dataset: "Data A",
        condition: "",
        precision: "reported",
        page: 2,
        evidence_ids: ["e2"],
        node_ids: ["n2"],
        conditions: [{ kind: "split", value: "held out", evidence_ids: ["e2"] }],
        missing_conditions: ["sample_size", "hardware"],
      },
    ],
    quality: { warnings: [] },
    run: { id: "run" },
    previews: [],
  } as unknown as DashboardReport;
}
