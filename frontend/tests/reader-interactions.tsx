import { createRoot } from "react-dom/client";
import { Reader } from "../src/Reader";
import { parseRoute, useHash } from "../src/router";
import type { DashboardReport, ReadingItem } from "../src/types";
import type { Prefs } from "../src/prefs";
import "../src/styles/index.css";

const bbox = { x0: 0.1, y0: 0.2, x1: 0.9, y1: 0.5 };
const paragraphs = Array.from({ length: 6 }, (_, n) => ({
  id: `b${n + 1}`,
  text: `Needle ${n + 1}. ${"A readable paragraph with enough text to retain a meaningful scroll position. ".repeat(12)}${n === 1 ? " Citation [2]." : ""}`,
  raw_text: "",
  bbox,
  page: 1,
  kind: "text",
  order: n,
  section: "Body",
}));
const references = [
  { id: "ref1", content: "Alpha source", reference_number: "1" },
  { id: "ref2", content: "Beta source", reference_number: "2" },
].map((ref) => ({
  ...ref,
  kind: "reference",
  parts: [],
  page: 1,
  bbox,
  label: "",
  level: 1,
  asset: "",
  visual_ids: [],
  result_ids: [],
}));
const items = paragraphs.map((b) => ({
  id: `reading-${b.id}`,
  kind: "paragraph",
  parts: [{ block_id: b.id, start: 0, end: b.text.length, join: "" }],
  page: 1,
  bbox,
  label: "",
  level: 1,
  asset: "",
  visual_ids: [],
  result_ids: [],
}));
const figure = {
  ...items[0],
  id: "fixture-figure",
  kind: "figure",
  label: "Figure 1",
  content: "An original crop",
  parts: [],
  asset:
    "data:image/svg+xml," +
    encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="320"><rect width="640" height="320" fill="skyblue"/><text x="50" y="100">Original image</text></svg>',
    ),
};
const report = {
  document: {
    title: "Interaction fixture",
    blocks: paragraphs,
    pages: [{ number: 1, label: "", width: 1000, height: 1400 }],
    warnings: [],
  },
  analysis: {
    title: "",
    nodes: [],
    warnings: [],
    glossary: [],
    reading_guide: [],
    paper_type: "research",
  },
  reading: {
    version: "2",
    source_version: "reader-interaction-fixture",
    items: [...items, figure] as ReadingItem[],
    references,
    information: {},
    hidden_ids: [],
    warnings: [],
  },
  graphs: [],
  evidence: [],
  previews: [],
  run: { id: "fixture", created_at: "", status: "complete", model: "offline-fixture" },
  quality: { warnings: [], grounded_nodes: 0, total_nodes: 0 },
  quantitative_results: [],
  visuals: [],
} as unknown as DashboardReport;
const prefs: Prefs = {
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
  mapDepth: "auto",
};
function Fixture() {
  const route = parseRoute(useHash());
  return (
    <Reader
      report={report}
      paperId="fixture"
      mode={route?.page === "read" ? route.mode : "text"}
      outlineSlot={null}
      prefs={prefs}
      onPrefs={() => {}}
      canExplain={false}
      onToast={() => {}}
    />
  );
}
location.hash = "#/read/fixture/text";
createRoot(document.getElementById("root")!).render(<Fixture />);
