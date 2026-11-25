import { createRoot } from "react-dom/client";
import { useState } from "react";
import { Reader } from "../src/Reader";
import { parseRoute, useHash } from "../src/router";
import type { DashboardReport } from "../src/types";
import type { Prefs } from "../src/prefs";
import "../src/styles/index.css";

const bbox = { x0: 0.1, y0: 0.2, x1: 0.9, y1: 0.5 };
const text =
  "Method A achieved 92% on Dataset A, using a held-out test split. Sample size and hardware need further source inspection.";
const nodes = [
  {
    id: "n1",
    title: "Research question",
    kind: "question",
    summary: "Existing research question",
    support: "supported",
    evidence_ids: ["e1"],
  },
  {
    id: "n2",
    title: "Limited conclusion",
    kind: "contribution",
    summary: "Existing contribution",
    support: "partial",
    evidence_ids: ["e1"],
  },
  {
    id: "n3",
    title: "Applicability boundary",
    kind: "limitation",
    summary: "Existing limitation",
    support: "supported",
    evidence_ids: ["e1"],
  },
].map((node) => ({
  ...node,
  explanation: "Read the original evidence before drawing conclusions.",
  parent_id: null,
  origin: "author",
}));
const report = {
  schema_version: "test",
  document: {
    title: "Review fixture",
    source_name: "fixture.pdf",
    sha256: "fixture-source",
    parser: "test",
    blocks: [
      {
        id: "b1",
        page: 1,
        text,
        raw_text: text,
        kind: "text",
        order: 0,
        section: "Results",
        bbox,
      },
    ],
    pages: [
      {
        number: 1,
        label: "1",
        width: 600,
        height: 800,
        status: "ok",
        text_chars: text.length,
        warnings: [],
      },
    ],
    warnings: [],
  },
  analysis: {
    title: "Review fixture",
    summary: "Existing one-sentence summary.",
    research_question: "What can Method A improve?",
    reading_guide: ["Read the result, then compare its conditions and limits."],
    nodes,
    edges: [
      { id: "edge", source: "n1", target: "n2", label: "supports", relation: "supports" },
    ],
    glossary: [],
    warnings: [],
    paper_type: "empirical",
  },
  evidence: [
    {
      id: "e1",
      block_id: "b1",
      page: 1,
      page_label: "1",
      section: "Results",
      quote: text,
      match: "exact",
      bbox,
    },
  ],
  reading: {
    version: "2",
    source_version: "fixture-source",
    items: [
      {
        id: "reading-b1",
        kind: "paragraph",
        parts: [{ block_id: "b1", start: 0, end: text.length, join: "" }],
        page: 1,
        bbox,
        label: "",
        level: 1,
        asset: "",
        visual_ids: [],
        result_ids: ["q1"],
      },
    ],
    references: [],
    information: {},
    hidden_ids: [],
    warnings: [],
  },
  graphs: [
    {
      id: "overview",
      title: "Overview map",
      description: "Default existing map",
      node_ids: nodes.map((n) => n.id),
      edge_ids: [],
      mermaid: "",
      validated: true,
    },
    {
      id: "argument",
      title: "Argument",
      description: "Existing argument",
      node_ids: nodes.map((n) => n.id),
      edge_ids: ["edge"],
      mermaid: "",
      validated: true,
    },
  ],
  previews: [
    {
      page: 1,
      width: 600,
      height: 800,
      data_url:
        "data:image/svg+xml," +
        encodeURIComponent(
          '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800"><rect width="600" height="800" fill="white"/><text x="60" y="180">Original result and experiment conditions</text></svg>',
        ),
    },
  ],
  quantitative_results: [
    {
      id: "q1",
      metric: "Accuracy",
      method: "Method A",
      dataset: "Dataset A",
      value: 92,
      value_text: "92",
      unit: "%",
      condition: "Held-out test split",
      page: 1,
      figure_label: "",
      source: "text",
      precision: "reported",
      evidence_ids: ["e1"],
      notes: "",
      conditions: [{ kind: "split", value: "held-out", evidence_ids: ["e1"] }],
      missing_conditions: ["sample_size", "hardware"],
      node_ids: ["n2"],
    },
  ],
  visuals: [],
  quality: {
    total_pages: 1,
    parsed_pages: 1,
    text_chars: text.length,
    grounded_nodes: 3,
    total_nodes: 3,
    checks: [],
    warnings: ["Unlocated processing hint"],
  },
  run: {
    id: "fixture-run-a",
    created_at: "2026-10-12T00:00:00Z",
    model: "offline-fixture",
    parser: "test",
    status: "complete",
    timings: {},
    usage: { requests: 0, prompt_tokens: 0, completion_tokens: 0 },
    prompt_version: "test",
  },
} as DashboardReport;
const prefs = {
  theme: "light",
  lightPalette: "default",
  darkPalette: "default",
  textSize: "standard",
  uiFontSize: 13,
  motion: "reduced",
  colorVision: "standard",
  showRelations: false,
  noteAIReview: false,
  sidebarCollapsed: false,
  mapDepth: "all",
} as Prefs;
function Fixture() {
  const route = parseRoute(useHash());
  const [second, setSecond] = useState(false);
  const current = second
    ? { ...report, run: { ...report.run, id: "fixture-run-b" } }
    : report;
  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <button data-switch-report onClick={() => setSecond(!second)}>
        Switch report revision
      </button>
      <Reader
        report={current}
        paperId="fixture"
        mode={route?.page === "read" ? route.mode : "map"}
        outlineSlot={null}
        prefs={prefs}
        onPrefs={() => {}}
        canExplain={false}
        onToast={() => {}}
      />
    </div>
  );
}
if (!location.hash) location.hash = "#/read/fixture";
createRoot(document.getElementById("root")!).render(<Fixture />);
